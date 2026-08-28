"""The 'new machine' command: init the DB, run the configured ingesters, report.

Idempotent. Safe to re-run: migrations only apply what is missing, ingesters
upsert, and cached HTTP responses mean a second run does no network I/O.

    uv run python scripts/bootstrap.py
    uv run python scripts/bootstrap.py --only anime_jikan --limit 100
    uv run python scripts/bootstrap.py --no-ingest      # schema only
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from recsys.config import get_settings  # noqa: E402
from recsys.db import (  # noqa: E402
    count_items,
    domain_stats,
    init_db,
    session,
)
from recsys.ingest.base import IngestResult  # noqa: E402
from recsys.ingest.registry import available, get_ingester  # noqa: E402
from recsys.log import get_logger, setup_logging  # noqa: E402

log = get_logger("recsys.bootstrap")


def table(headers: list[str], rows: list[list[str]]) -> str:
    """Minimal fixed-width table. No dependency worth taking for this."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    sep = "  ".join("-" * w for w in widths)
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)), sep]
    out += ["  ".join(c.ljust(widths[i]) for i, c in enumerate(row)) for row in rows]
    return "\n".join(out)


async def run_ingesters(names: list[str], limit: int | None) -> list[IngestResult]:
    settings = get_settings()
    results: list[IngestResult] = []

    for name in names:
        ingester = get_ingester(name, settings)
        if limit is not None and hasattr(ingester, "target_count"):
            ingester.target_count = limit

        target = getattr(ingester, "target_count", "?")
        print(f"\n>> {name}  (domain={ingester.domain}, target={target})")
        with session() as conn:
            results.append(await ingester.run(conn))

    return results


def report(results: list[IngestResult]) -> None:
    settings = get_settings()

    if results:
        print("\nIngest")
        print(table(
            ["source", "domain", "fetched", "inserted", "updated", "cached", "skipped",
             "secs", "status"],
            [[
                r.source, r.domain, str(r.n_fetched), str(r.n_inserted), str(r.n_updated),
                str(r.n_cached), str(r.n_skipped), f"{r.duration_s:.1f}", r.status,
            ] for r in results],
        ))
        for r in results:
            if r.error:
                print(f"\n  !! {r.source}: {r.error}")

    with session() as conn:
        total = count_items(conn)
        stats = domain_stats(conn)

    print("\nCatalog")
    if stats:
        print(table(
            ["domain", "items", "sources", "last ingest"],
            [[
                s["domain"], str(s["n_items"]), ",".join(s["sources"]),
                (s["last_ingest_at"] or "-")[:19],
            ] for s in stats],
        ))
    else:
        print("  (empty)")

    print(f"\n  total items : {total}")
    print(f"  database    : {settings.db_path}")
    print(f"  cache       : {settings.cache_dir}")
    print(f"  region      : {settings.region}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialise and populate the catalog")
    parser.add_argument("--only", action="append", metavar="NAME",
                        help=f"run only this ingester (repeatable). Available: {available()}")
    parser.add_argument("--limit", type=int, default=None,
                        help="override the per-ingester target item count")
    parser.add_argument("--no-ingest", action="store_true",
                        help="initialise the schema and report, but fetch nothing")
    args = parser.parse_args(argv)

    setup_logging()
    settings = get_settings()
    settings.ensure_dirs()

    version = init_db()
    print(f"Schema ready at {settings.db_path} (v{version})")

    results: list[IngestResult] = []
    if not args.no_ingest:
        names = args.only or settings.enabled_ingesters
        unknown = [n for n in names if n not in available()]
        if unknown:
            parser.error(f"unknown ingester(s) {unknown}; available: {available()}")
        results = asyncio.run(run_ingesters(names, args.limit))

    report(results)
    return 1 if any(r.status != "ok" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
