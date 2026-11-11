"""Apply a mapper fix to rows already in the catalog, without re-fetching them.

    uv run python scripts/remap.py --only book_openlibrary --field image_url
    uv run python scripts/remap.py --only book_goodbooks --field image_url --dry-run

Reads each row's stored ``raw_json`` back through its ingester's ``map_item``
and rewrites only the named columns -- see ``recsys/remap.py`` for why the
columns must be named. No network. Follow it with
``scripts/bootstrap.py --no-ingest`` to rebuild the derived tables.
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
from recsys.db import init_db, session
from recsys.ingest.registry import available, get_ingester
from recsys.log import setup_logging
from recsys.remap import REMAPPABLE, remap


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-map stored payloads, offline")
    parser.add_argument("--only", action="append", metavar="NAME", required=True,
                        help=f"ingester whose rows to remap (repeatable). Available: {available()}")
    parser.add_argument("--field", action="append", metavar="COLUMN", required=True,
                        choices=REMAPPABLE,
                        help=f"column to rewrite (repeatable). One of: {', '.join(REMAPPABLE)}")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, and change nothing")
    args = parser.parse_args(argv)

    unknown = [n for n in args.only if n not in available()]
    if unknown:
        parser.error(f"unknown ingester(s) {unknown}; available: {available()}")

    setup_logging()
    settings = get_settings()
    settings.ensure_dirs()
    init_db()

    for name in args.only:
        ingester = get_ingester(name, settings)
        if not hasattr(ingester, "map_item"):
            parser.error(f"{name} loads no catalog rows, so it has nothing to remap")
        with session() as conn:
            result = remap(conn, ingester, args.field, dry_run=args.dry_run)
        verb = "would change" if result.dry_run else "changed"
        print(f"{name}: {result.n_rows} rows, {result.n_changed} {verb} "
              f"({result.n_emptied} to empty), {result.n_unmapped} no longer map")

    if not args.dry_run:
        print("\nNow rebuild the derived tables:  "
              "uv run python scripts/bootstrap.py --no-ingest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
