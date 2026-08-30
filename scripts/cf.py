"""Build the item-item collaborative model from the ratings corpus.

Separate from bootstrap.py for the same reason ``embed.py`` is: this is a model
build, not an ingest. It reads 19.2M ratings in one full scan, writes one
parquet artifact under ``DATA_DIR``, and touches ``items`` not at all.

    uv run python scripts/cf.py
    uv run python scripts/cf.py --min-raters 50    # a stricter floor
    uv run python scripts/cf.py --evaluate         # held-out recall vs popularity
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from recsys.config import get_settings  # noqa: E402
from recsys.db import session  # noqa: E402
from recsys.log import get_logger, setup_logging  # noqa: E402
from recsys.models.collaborative import (  # noqa: E402
    build_collaborative,
    report_held_out_recall,
)

log = get_logger("recsys.cf")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the collaborative model")
    parser.add_argument("--k", type=int, default=50,
                        help="neighbours kept per item (default 50)")
    parser.add_argument("--min-raters", type=int, default=20,
                        help="drop items with fewer raters than this (default 20)")
    parser.add_argument("--shrinkage", type=float, default=50.0,
                        help="pull similarity toward zero for thinly co-rated pairs")
    parser.add_argument("--evaluate", action="store_true",
                        help="score the existing model instead of rebuilding it")
    args = parser.parse_args(argv)

    setup_logging()
    settings = get_settings()
    settings.ensure_dirs()
    out = settings.collaborative_path

    if args.evaluate:
        with session() as conn:
            return report_held_out_recall(conn, out)

    with session() as conn:
        result = build_collaborative(
            conn, out_path=out, k=args.k,
            min_raters=args.min_raters, shrinkage=args.shrinkage,
        )

    if not result.n_pairs:
        print("No neighbours were produced. Is the ratings table populated?")
        return 1

    print(f"\nCollaborative model written to {out}")
    print(f"  ratings read      : {result.n_ratings:,}")
    print(f"  users             : {result.n_users:,}")
    print(f"  items kept        : {result.n_items:,} "
          f"({result.dropped_items:,} dropped under min_raters={args.min_raters})")
    print(f"  neighbour pairs   : {result.n_pairs:,} "
          f"({result.neighbours_per_item:.1f} per item)")
    print(f"  build time        : {result.build_seconds:.1f}s")
    print("\nScore it with:  uv run python scripts/cf.py --evaluate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
