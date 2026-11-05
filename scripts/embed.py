"""Build the text-embedding matrix for the catalog.

Separate from bootstrap.py on purpose: this is a model-build step, not an
ingest. It is expensive, it is rebuilt when the catalog changes rather than on
every run, and it needs a model that bootstrap has no business downloading.

    uv run python scripts/embed.py                 # embed only what changed
    uv run python scripts/embed.py --check         # say what changed, embed nothing
    uv run python scripts/embed.py --full          # embed every work again
    uv run python scripts/embed.py --evaluate      # score it against known links

A rebuild re-embeds only works whose document changed or that are new: each
vector records a fingerprint of the document and model it came from. A matrix
built before fingerprints is kept and reported as unverified; ``--full``
replaces it.

The model is fetched once into DATA_DIR and cached there. fastembed defaults to
a temporary directory, which Windows clears -- so the cache location is set
explicitly rather than left to be silently re-downloaded.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from recsys.config import get_settings
from recsys.db import session
from recsys.log import get_logger, setup_logging
from recsys.models.text import Freshness, build_embeddings, matrix_freshness
from recsys.net import enable_os_truststore

log = get_logger("recsys.embed")


def make_embedder(settings, limit: int | None = None):
    """A fastembed backend, loaded lazily so importing this module is cheap."""
    enable_os_truststore()  # the model download goes through the same proxy
    from fastembed import TextEmbedding

    cache = settings.data_dir / "models"
    cache.mkdir(parents=True, exist_ok=True)
    threads = settings.embedding_threads or os.cpu_count() or 1
    log.info("loading %s (cached in %s, %d threads)",
             settings.embedding_model, cache, threads)
    model = TextEmbedding(settings.embedding_model, cache_dir=str(cache), threads=threads)

    def embed(texts):
        return model.embed(
            list(texts),
            batch_size=settings.embedding_batch_size,
            parallel=settings.embedding_parallel,
        )

    return embed


def with_progress(embed, total: int, every: int = 2000):
    """Wrap an embedder so it prints how far it is and how long is left.

    The embedder is a parameter for exactly this kind of reason: progress is a
    concern of the command, not of the matrix build.
    """
    started = time.monotonic()
    done = 0
    last = 0

    def wrapped(texts):
        nonlocal done, last
        out = list(embed(texts))
        done += len(out)
        if total and (done - last >= every or done >= total):
            last = done
            elapsed = time.monotonic() - started
            left = elapsed / done * (total - done) if done else 0.0
            print(f"  embedded {done:,}/{total:,}  ({100 * done / total:.0f}%)  "
                  f"elapsed {_clock(elapsed)}  left ~{_clock(left)}", flush=True)
        return out

    return wrapped


def _clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def describe(fresh: Freshness) -> str:
    """One line a person can act on."""
    if fresh.is_fresh:
        return f"fresh: all {fresh.n_works:,} works have a vector made from today's text"
    parts = [f"{fresh.n_current:,} current"]
    for n, what in ((fresh.n_changed, "changed"), (fresh.n_missing, "missing"),
                    (fresh.n_orphaned, "orphaned"),
                    (fresh.n_unknown, "unverified (built before fingerprints)")):
        if n:
            parts.append(f"{n:,} {what}")
    return f"stale: {', '.join(parts)} of {fresh.n_works:,} works; {fresh.n_to_embed:,} to embed"


def evaluate(settings) -> int:
    """Score the embeddings against links the catalog already knows are true.

    Adaptation edges are ground truth nobody had to label: an anime and the
    manga it was adapted from are the same story in two media. If the model
    cannot put those near each other it will not connect an anime to an
    unrelated novel either, which is the thing it exists to do.
    """
    from recsys.models.evaluate import report_adaptation_recall

    with session() as conn:
        return report_adaptation_recall(conn, settings.embeddings_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the catalog embedding matrix")
    parser.add_argument("--limit", type=int, default=None,
                        help="embed only the first N works (development)")
    parser.add_argument("--evaluate", action="store_true",
                        help="skip building; score the existing matrix against adaptation links")
    parser.add_argument("--check", action="store_true",
                        help="report what changed since the matrix was built, embed "
                             "nothing; exit 1 if anything is stale")
    parser.add_argument("--full", action="store_true",
                        help="embed every work again instead of only what changed")
    args = parser.parse_args(argv)

    setup_logging()
    settings = get_settings()
    settings.ensure_dirs()

    if args.evaluate:
        return evaluate(settings)

    with session() as conn:
        fresh = matrix_freshness(conn, settings.embeddings_path,
                                 model=settings.embedding_model)
    print(describe(fresh))
    if args.check:
        return 0 if fresh.is_fresh else 1
    if fresh.is_fresh and not args.full:
        return 0

    total = fresh.n_works if args.full else fresh.n_to_embed
    embedder = with_progress(make_embedder(settings), total)
    with session() as conn:
        result = build_embeddings(
            conn,
            embedder,
            out_path=settings.embeddings_path,
            batch_size=settings.embedding_batch_size,
            model=settings.embedding_model,
            reuse=not args.full,
        )

    print(f"\nEmbedded {result.n_embedded} works, kept {result.n_reused}; "
          f"{result.n_documents} in {result.dim} dimensions")
    for domain, n in sorted(result.by_domain.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>7}  {domain}")
    if result.n_skipped:
        print(f"  {result.n_skipped:>7}  skipped (no text at all)")
    print(f"\n  matrix: {settings.embeddings_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
