"""The full build: every source, the ratings, the models -- one resumable command.

    uv run python scripts/build.py            # start, or carry on where it stopped
    uv run python scripts/build.py --status   # what is done, what is left, how long

A full build is hours of polite requests (MusicBrainz at under one a second,
Open Library at three) plus a multi-hour embedding run, so it has to survive
being interrupted. Each step records its outcome in ``DATA_DIR/build-state.json``
when it finishes; running again skips what succeeded and retries what did not.
Every step underneath is idempotent anyway -- ingesters upsert, responses are
cached -- so a step cut off half way costs only its unfinished part.

**It asks for credentials only when a step needs them.** TMDB needs a key
(films, television, and the composers music is built from); it is asked for
once and written to ``.env``, the one place credentials live. The MAL dump is
downloaded through kagglehub, which fetches public datasets without a login;
only if that download is refused does the build ask you to log in to Kaggle.
Without a terminal to ask in, a step that needs a credential is skipped and
reported, and everything else still runs.

**It checks before it pays.** The embedding matrix is rebuilt only for works
whose text changed (``models/text.py`` fingerprints every vector), and not at
all when nothing did. The collaborative model is rebuilt only when the
ratings changed.

**TMDB data is kept under six months old.** TMDB's terms forbid storing API
data for longer, so a TMDB step that succeeded more than ``tmdb_max_age_days``
ago counts as not done, and the next build fetches it again.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recsys.log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Step:
    name: str
    label: str
    #: Seconds, from the timings quoted in README/ROADMAP; refined by the
    #: step's own last duration once it has run on this machine.
    estimate: float
    needs: str | None = None  # "tmdb" | "kaggle"
    kind: str = "ingest"  # "ingest" | "derive" | "embed" | "cf"


#: In dependency order: AniList enriches the MAL ids the dump wrote, manga
#: follows anime's adaptation edges, music follows TMDB's crew credits, and
#: everything derived comes after every source.
STEPS: tuple[Step, ...] = (
    Step("book_goodbooks", "Books (goodbooks-10k)", 15),
    Step("book_goodbooks_ratings", "6M book ratings (goodbooks-10k)", 90),
    Step("book_openlibrary", "Book synopses and covers (Open Library, 3/s)", 3900),
    Step("anime_mal_csv", "Anime (MyAnimeList Kaggle dump)", 120, needs="kaggle"),
    Step("anime_anilist", "Anime synopses and tags (AniList, 30/min)", 1200),
    Step("manga_anilist", "Manga (AniList, 30/min)", 1800),
    Step("movie_tmdb", "Films (TMDB)", 1800, needs="tmdb"),
    Step("tv_tmdb", "Television (TMDB)", 1500, needs="tmdb"),
    Step("music_musicbrainz", "Composers (MusicBrainz under 1/s, Deezer)", 9000, needs="tmdb"),
    Step("idmap_wikidata", "Anime <-> TMDB id map (Wikidata)", 15),
    Step("anime_mal_ratings", "19.2M anime ratings (MyAnimeList Kaggle dump)", 90,
         needs="kaggle"),
    Step("derive", "Link, calibrate, relate, index", 30, kind="derive"),
    Step("embed", "Embedding matrix (only what changed)", 9000, kind="embed"),
    Step("cf", "Collaborative model", 150, kind="cf"),
)

TMDB_STEPS = frozenset({"movie_tmdb", "tv_tmdb"})


def clock(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


@dataclass
class State:
    """What each step last did, persisted between runs."""

    path: Path
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        return cls(Path(path), dict(data.get("steps", {})))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"steps": self.steps}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def record(self, step: str, status: str, seconds: float, note: str = "", **extra: Any) -> None:
        self.steps[step] = {
            "status": status,
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "seconds": round(seconds, 1),
            "note": note,
            **extra,
        }
        self.save()

    def seed(self, finished: dict[str, str]) -> None:
        """Count steps an earlier bootstrap already did as done.

        ``finished`` maps a step to when its source's last successful ingest
        finished (from ``ingest_runs``). Only steps this file has no record of
        are seeded, so the first ``build.py`` on an existing catalog does not
        start from nothing -- and TMDB's six-month clock starts from the real
        fetch, not from today.
        """
        for name, finished_at in finished.items():
            if name not in self.steps:
                self.steps[name] = {"status": "ok", "finished_at": finished_at,
                                    "seconds": None, "note": "done by an earlier bootstrap"}

    def done(self, step: Step, *, now: datetime, tmdb_max_age_days: int) -> bool:
        entry = self.steps.get(step.name)
        if not entry or entry.get("status") != "ok":
            return False
        if step.name in TMDB_STEPS:
            finished = datetime.fromisoformat(entry["finished_at"])
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=UTC)
            if (now - finished).days >= tmdb_max_age_days:
                return False
        return True

    def estimate(self, step: Step) -> float:
        """This machine's last real duration for the step, else the quoted one.

        A run that did nothing -- a fresh matrix, a skipped step -- says
        nothing about how long the work takes, so it does not count.
        """
        entry = self.steps.get(step.name, {})
        if entry.get("status") == "ok" and entry.get("seconds") is not None:
            return float(entry["seconds"])
        return step.estimate


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------


def set_env_value(env_path: Path, key: str, value: str, example: Path | None = None) -> None:
    """Write ``KEY=value`` into ``.env``, replacing an existing line for it.

    ``.env`` is created from ``.env.example`` if it does not exist, so the
    file gains the documented defaults rather than one bare line. The value is
    never logged.
    """
    env_path = Path(env_path)
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    elif example is not None and Path(example).is_file():
        lines = Path(example).read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    out, replaced = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key:
            if not replaced:
                out.append(f"{key}={value}")
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    tmp = env_path.with_name(env_path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, env_path)  # never a half-written .env


#: How a kagglehub refusal to download without a login reads in an error.
_KAGGLE_AUTH_MARKS = ("UnauthenticatedError", "CredentialError", "401", "403", "credential")


def kaggle_refused_login(error: str | None) -> bool:
    """Whether an ingest error is Kaggle asking for a login, and nothing else.

    A missing local file (MAL_CSV_PATH) or a network failure is not; offering a
    login for those would be answering the wrong question.
    """
    return bool(error) and any(mark in (error or "") for mark in _KAGGLE_AUTH_MARKS)


def cf_is_fresh(last: dict[str, Any], *, model_exists: bool, n_ratings: int) -> bool:
    """The collaborative model is current: built, successfully, from these ratings."""
    return (model_exists and last.get("status") in ("ok", "fresh")
            and last.get("n_ratings") == n_ratings)


def has_kaggle_credentials(env: dict[str, str] | None = None, home: Path | None = None) -> bool:
    """Whether kagglehub could authenticate without asking."""
    env = dict(os.environ) if env is None else env
    home = Path.home() if home is None else home
    if env.get("KAGGLE_API_TOKEN") or (env.get("KAGGLE_USERNAME") and env.get("KAGGLE_KEY")):
        return True
    return any((home / ".kaggle" / name).is_file() for name in ("kaggle.json", "access_token"))


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


@dataclass
class Outcome:
    status: str  # "ok" | "error" | "skipped" | "fresh"
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


#: Runs one step: (step) -> Outcome. The CLI supplies the real one.
Runner = Callable[[Step], Outcome]


@dataclass
class Build:
    state: State
    run_step: Runner
    tmdb_configured: Callable[[], bool]
    #: Returns the TMDB read token typed, or "" to skip. None = cannot ask.
    ask_tmdb: Callable[[], str] | None
    save_tmdb: Callable[[str], None]
    #: Asks to log in to Kaggle and does so; True if it did. None = cannot ask.
    ask_kaggle: Callable[[], bool] | None
    kaggle_configured: Callable[[], bool]
    tmdb_max_age_days: int = 180
    print: Callable[[str], None] = print
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def plan(self, redo: set[str] | None = None, skip: set[str] | None = None) -> list[Step]:
        redo, skip = redo or set(), skip or set()
        now = self.now()
        return [
            s for s in STEPS
            if s.name not in skip
            and (s.name in redo or s.kind in ("embed", "cf", "derive")
                 or not self.state.done(s, now=now, tmdb_max_age_days=self.tmdb_max_age_days))
        ]

    def status(self) -> list[tuple[Step, str, float]]:
        """(step, what it is, seconds it is expected to take) for every step."""
        now = self.now()
        out = []
        for step in STEPS:
            entry = self.state.steps.get(step.name)
            if self.state.done(step, now=now, tmdb_max_age_days=self.tmdb_max_age_days):
                what = f"done {entry['finished_at'][:10]}" if entry else "done"
            elif entry and entry.get("status") == "ok" and step.name in TMDB_STEPS:
                what = f"older than {self.tmdb_max_age_days} days: will refetch (TMDB's terms)"
            elif entry:
                what = f"{entry['status']}: {entry.get('note', '')}".rstrip(": ")
            else:
                what = "not run"
            out.append((step, what, self.state.estimate(step)))
        return out

    def run(self, redo: set[str] | None = None, skip: set[str] | None = None) -> int:
        steps = self.plan(redo, skip)
        started = time.monotonic()
        skipped_tmdb = False
        failures = 0
        total_estimate = sum(self.state.estimate(s) for s in steps)
        spent_on_estimated = estimated_done = 0.0

        for n, step in enumerate(steps, start=1):
            remaining = sum(self.state.estimate(s) for s in steps[n - 1:])
            # Scale what is left by how this machine has done so far.
            pace = spent_on_estimated / estimated_done if estimated_done else 1.0
            pace = min(max(pace, 0.5), 3.0)
            self.print(
                f"\n[{n}/{len(steps)}] {step.label}  ·  est. {clock(self.state.estimate(step))}"
                f"  ·  elapsed {clock(time.monotonic() - started)}"
                f"  ·  left ~{clock(remaining * pace)} of ~{clock(total_estimate)}"
            )

            if step.needs == "tmdb" and not self._have_tmdb(skipped_tmdb):
                skipped_tmdb = True
                self.state.record(step.name, "skipped", 0.0, "needs a TMDB key")
                self.print("    skipped: needs a TMDB key (TMDB_READ_TOKEN in .env)")
                continue

            expected = self.state.estimate(step)
            t0 = time.monotonic()
            outcome = self._run(step)
            if (outcome.status == "error" and step.needs == "kaggle"
                    and outcome.extra.get("needs_login") and self.ask_kaggle is not None):
                self.print("    Kaggle refused the download without a login.")
                if self.ask_kaggle():
                    outcome = self._run(step)
            elapsed = time.monotonic() - t0
            self.state.record(step.name, outcome.status, elapsed, outcome.note, **outcome.extra)
            if outcome.status == "ok":
                spent_on_estimated += elapsed
                estimated_done += expected
            if outcome.status == "error":
                failures += 1
            self.print(f"    {outcome.status}" + (f": {outcome.note}" if outcome.note else "")
                       + f"  ({clock(elapsed)})")

        self.print(f"\nFinished in {clock(time.monotonic() - started)}; "
                   f"{failures} step(s) failed. Run again to retry them.")
        skipped = [s.name for s in steps if self.state.steps.get(s.name, {}).get("status")
                   == "skipped"]
        if skipped:
            self.print(f"Skipped: {', '.join(skipped)}. Add TMDB_READ_TOKEN to .env (or run "
                       "again in a terminal to be asked) and run again to include them.")
        return 1 if failures else 0

    def _run(self, step: Step) -> Outcome:
        """One step, whatever it raises: a failure is recorded, not a crash."""
        try:
            return self.run_step(step)
        except Exception as exc:  # one broken step must not end a long build
            log.exception("step %s raised", step.name)
            return Outcome("error", f"{type(exc).__name__}: {exc}")

    def _have_tmdb(self, already_declined: bool) -> bool:
        if self.tmdb_configured():
            return True
        if already_declined or self.ask_tmdb is None:
            return False
        token = self.ask_tmdb().strip()
        if not token:
            return False
        self.save_tmdb(token)
        return self.tmdb_configured()
