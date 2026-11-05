"""Build the starter pack from a catalog. The maintainer's side of --starter.

    uv run python scripts/starter.py --version v1          # into DATA_DIR/dist

Reads DATA_DIR (or --data-dir) and never writes to it: the shippable rows,
the vectors for their works and a collaborative model trained on the
shippable ratings go into a new archive with a manifest, an attribution file
and a .sha256 beside it. What counts as shippable is `recsys.starter.SHIPPABLE`
-- read it, and the source terms it cites, before publishing anything.

Publishing the archive (a GitHub Release asset) is a separate, deliberate
step this script does not take. Pin the printed checksum in STARTER_SHA256.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from recsys.config import get_settings
from recsys.derive import rebuild_derived
from recsys.log import setup_logging
from recsys.models.collaborative import build_collaborative
from recsys.starter import SHIPPABLE, build_pack


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Loom starter pack")
    parser.add_argument("--version", required=True, help="pack version, e.g. v1")
    parser.add_argument("--out", type=Path, default=None,
                        help="where the archive is written (default: DATA_DIR/dist -- "
                             "never inside the repository)")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="the catalog to build from (default: DATA_DIR)")
    parser.add_argument("--no-embeddings", action="store_true",
                        help="build without vectors (For you and More like this "
                             "will ask for scripts/embed.py)")
    args = parser.parse_args(argv)

    setup_logging()
    data_dir = args.data_dir or get_settings().data_dir
    out = args.out or get_settings().data_dir / "dist"
    source = data_dir / "recsys.db"
    matrix = data_dir / "features" / "embeddings.parquet"
    if not source.is_file():
        print(f"no catalog at {source}")
        return 1
    if not args.no_embeddings and not matrix.is_file():
        print(f"no embedding matrix at {matrix}; run scripts/embed.py or pass --no-embeddings")
        return 1
    out.mkdir(parents=True, exist_ok=True)

    print("Building the starter pack from", source)
    for (domain, src), terms in SHIPPABLE.items():
        print(f"  ships {domain}:{src} -- {terms}")
    result = build_pack(
        source,
        embeddings=None if args.no_embeddings else matrix,
        out_dir=out,
        version=args.version,
        rebuild_derived=rebuild_derived,
        build_collaborative=lambda conn, out: build_collaborative(conn, out_path=out),
    )
    size = result.archive.stat().st_size / 1e6
    print(f"\n  archive : {result.archive} ({size:.1f} MB)")
    print(f"  sha256  : {result.sha256}")
    for name, n in sorted(result.counts.items()):
        print(f"  {name:<18} {n:,}")
    if result.missing_vectors:
        print(f"  !! {result.missing_vectors} works have no vector; rebuild the matrix first")
    print("\nPin it: STARTER_SHA256=" + result.sha256)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
