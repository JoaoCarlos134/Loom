"""Build the text-embedding matrix for the catalog.

Separate from bootstrap.py on purpose: this is a model-build step, not an
ingest. It is expensive, it is rebuilt when the catalog changes rather than on
every run, and it needs a model that bootstrap has no business downloading.

    uv run python scripts/embed.py
    uv run python scripts/embed.py --limit 500     # a quick pass while developing
    uv run python scripts/embed.py --evaluate      # score it against known links

The model is fetched once into DATA_DIR and cached there. fastembed defaults to
a temporary directory, which Windows clears -- so the cache location is set
explicitly rather than left to be silently re-downloaded.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from recsys.config import get_settings  # noqa: E402
from recsys.db import session  # noqa: E402
from recsys.log import get_logger, setup_logging  # noqa: E402
from recsys.models.text import build_embeddings  # noqa: E402
from recsys.net import enable_os_truststore  # noqa: E402

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
    args = parser.parse_args(argv)

    setup_logging()
    settings = get_settings()
    settings.ensure_dirs()

    if args.evaluate:
        return evaluate(settings)

    embedder = make_embedder(settings)
    with session() as conn:
        result = build_embeddings(
            conn,
            embedder,
            out_path=settings.embeddings_path,
            batch_size=settings.embedding_batch_size,
        )

    print(f"\nEmbedded {result.n_documents} works into {result.dim} dimensions")
    for domain, n in sorted(result.by_domain.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>7}  {domain}")
    if result.n_skipped:
        print(f"  {result.n_skipped:>7}  skipped (no text at all)")
    print(f"\n  matrix: {settings.embeddings_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
