"""The full build, in one resumable command. See ``recsys/build.py`` for the rules.

    uv run python scripts/build.py              # start, or carry on where it stopped
    uv run python scripts/build.py --status     # what is done, what is left, how long
    uv run python scripts/build.py --redo anime_anilist
    uv run python scripts/build.py --skip music_musicbrainz
    uv run python scripts/build.py --no-input   # never ask; skip what needs a credential

Every step's own progress is logged as it runs; between steps this prints how
far the build is and roughly how long is left, refined by how long each step
took on this machine the last time it ran.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from recsys.build import (
    STEPS,
    Build,
    Outcome,
    State,
    Step,
    cf_is_fresh,
    clock,
    has_kaggle_credentials,
    kaggle_refused_login,
)
from recsys.build import set_env_value as _set_env_value
from recsys.config import REPO_ROOT, get_settings
from recsys.db import init_db, session
from recsys.derive import rebuild_derived
from recsys.ingest.registry import get_ingester
from recsys.log import setup_logging


def run_step(step: Step, *, embed_full: bool, state: State) -> Outcome:
    settings = get_settings()
    if step.kind == "ingest":
        runner = get_ingester(step.name, settings)
        with session() as conn:
            result = asyncio.run(runner.run(conn))
        if result.status != "ok":
            explicit = settings.mal_csv_path if step.name == "anime_mal_csv" else (
                settings.mal_ratings_path if step.name == "anime_mal_ratings" else "")
            needs_login = step.needs == "kaggle" and not explicit and kaggle_refused_login(
                result.error)
            return Outcome("error", result.error or result.status, {"needs_login": needs_login})
        return Outcome("ok", f"{result.n_fetched:,} read, {result.n_inserted:,} new, "
                             f"{result.n_updated:,} updated")
    if step.kind == "derive":
        init_db()
        with session() as conn:
            derived = rebuild_derived(conn)
        return Outcome("ok", f"{derived.works.n_works:,} works, {derived.links.n_aliased:,} "
                             f"joined through id_map, {derived.works.n_groups:,} groups")
    if step.kind == "embed":
        from recsys.models.text import matrix_freshness

        with session() as conn:
            fresh = matrix_freshness(conn, settings.embeddings_path,
                                     model=settings.embedding_model)
        if fresh.is_fresh and not embed_full:
            return Outcome("fresh", f"all {fresh.n_works:,} works current; nothing to embed")
        if not fresh.n_to_embed and not embed_full and not fresh.n_orphaned:
            return Outcome("fresh", f"{fresh.n_unknown:,} vectors predate fingerprints and "
                                    "are kept; --embed-full rebuilds them")
        args = [sys.executable, str(ROOT / "scripts" / "embed.py")]
        if embed_full:
            args.append("--full")
        code = subprocess.call(args)
        n = fresh.n_works if embed_full else fresh.n_to_embed
        return Outcome("ok" if code == 0 else "error",
                       f"{n:,} works embedded" if code == 0 else f"exit {code}")
    if step.kind == "cf":
        with session() as conn:
            n = int(conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0])
        last = state.steps.get("cf", {})
        if cf_is_fresh(last, model_exists=settings.collaborative_path.is_file(), n_ratings=n):
            return Outcome("fresh", f"ratings unchanged ({n:,})", {"n_ratings": n})
        if not n:
            return Outcome("skipped", "no ratings to learn from")
        code = subprocess.call([sys.executable, str(ROOT / "scripts" / "cf.py")])
        if code != 0:
            return Outcome("error", f"exit {code}")
        return Outcome("ok", f"{n:,} ratings", {"n_ratings": n})
    if step.kind == "retention":
        from recsys.ingest.cache import DiskCache
        from recsys.prune import expire

        with session() as conn:
            expired = expire(conn, settings, dry_run=False)
        days = settings.tmdb_max_age_days
        purged = DiskCache(settings.cache_dir, "tmdb").purge(days * 86400)
        return Outcome("ok", f"{expired.deleted:,} TMDB rows and {purged:,} cached TMDB "
                             f"responses older than {days} days removed")
    raise ValueError(f"unknown step kind {step.kind!r}")


def tmdb_configured() -> bool:
    get_settings.cache_clear()
    s = get_settings()
    return bool(s.tmdb_read_token or s.tmdb_api_key)


def ask_tmdb() -> str:
    print("\n    Films, television and the composers music is built from come from TMDB,")
    print("    which needs a free API read access token:")
    print("    https://www.themoviedb.org/settings/api  (the 'API Read Access Token')")
    return getpass.getpass("    Paste it here (input hidden), or press Enter to skip: ")


def save_tmdb(token: str) -> None:
    _set_env_value(REPO_ROOT / ".env", "TMDB_READ_TOKEN", token, example=REPO_ROOT / ".env.example")
    print(f"    saved to {REPO_ROOT / '.env'} (the one place Loom keeps credentials)")


def ask_kaggle() -> bool:
    print("    The MyAnimeList dump downloads through your Kaggle account. Create an API")
    print("    token at https://www.kaggle.com/settings (API -> Create New Token).")
    token = getpass.getpass("    Paste it here (input hidden), or press Enter to skip: ").strip()
    if not token:
        return False
    _set_env_value(REPO_ROOT / ".env", "KAGGLE_API_TOKEN", token,
                   example=REPO_ROOT / ".env.example")
    get_settings.cache_clear()
    print(f"    saved to {REPO_ROOT / '.env'} (the one place Loom keeps credentials)")
    return True


def finished_ingests() -> dict[str, str]:
    """Each ingest step's last successful run, from ingest_runs -- read-only."""
    import sqlite3

    path = get_settings().db_path
    if not path.is_file():
        return {}
    by_source = {}
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        for source, domain, finished in conn.execute(
            "SELECT source, domain, MAX(finished_at) FROM ingest_runs "
            "WHERE status = 'ok' GROUP BY source, domain"
        ):
            by_source[(source, domain)] = finished
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out = {}
    for step in STEPS:
        if step.kind != "ingest":
            continue
        runner = get_ingester(step.name, get_settings())
        finished = by_source.get((runner.source, runner.domain))
        if finished:
            out[step.name] = finished
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the whole catalog, resumably")
    parser.add_argument("--status", action="store_true", help="report and run nothing")
    parser.add_argument("--redo", action="append", default=[], metavar="STEP",
                        help="run a finished step again (repeatable)")
    parser.add_argument("--skip", action="append", default=[], metavar="STEP",
                        help="leave a step out of this run (repeatable)")
    parser.add_argument("--restart", action="store_true",
                        help="forget what earlier runs finished (data is kept)")
    parser.add_argument("--embed-full", action="store_true",
                        help="re-embed every work, not only those whose text changed")
    parser.add_argument("--no-input", action="store_true",
                        help="never prompt; skip steps whose credentials are missing")
    args = parser.parse_args(argv)

    names = {s.name for s in STEPS}
    unknown = sorted((set(args.redo) | set(args.skip)) - names)
    if unknown:
        parser.error(f"unknown step(s) {unknown}; steps: {[s.name for s in STEPS]}")

    setup_logging()
    settings = get_settings()
    settings.ensure_dirs()
    if not args.status:  # --status reads; it does not migrate anything
        init_db()
    state_path = settings.data_dir / "build-state.json"
    if args.restart:
        state_path.unlink(missing_ok=True)
    state = State.load(state_path)
    if not args.restart:
        state.seed(finished_ingests())
    interactive = sys.stdin.isatty() and not args.no_input

    build = Build(
        state=state,
        run_step=lambda step: run_step(step, embed_full=args.embed_full, state=state),
        tmdb_configured=tmdb_configured,
        ask_tmdb=ask_tmdb if interactive else None,
        save_tmdb=save_tmdb,
        ask_kaggle=ask_kaggle if interactive else None,
        kaggle_configured=has_kaggle_credentials,
        tmdb_max_age_days=settings.tmdb_max_age_days,
    )

    if args.status:
        total = 0.0
        for step, what, seconds in build.status():
            todo = not what.startswith("done")
            total += seconds if todo else 0.0
            print(f"  {'·' if not todo else '>'} {step.name:<24} {what:<52} ~{clock(seconds)}")
        print(f"\n  left: ~{clock(total)} (embed and cf check their own freshness first)")
        return 0

    print(f"Full build into {settings.data_dir}")
    return build.run(redo=set(args.redo), skip=set(args.skip))


if __name__ == "__main__":
    raise SystemExit(main())
