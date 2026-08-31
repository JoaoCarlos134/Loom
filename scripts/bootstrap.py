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

from recsys.config import get_settings
from recsys.db import (
    count_items,
    count_rows,
    domain_stats,
    init_db,
    session,
)
from recsys.ingest.base import IngestResult
from recsys.ingest.registry import available, get_ingester
from recsys.link import LinkResult, rebuild_links
from recsys.log import get_logger, setup_logging
from recsys.popularity import PopularityResult, rebuild_popularity
from recsys.prune import prune
from recsys.relations import RelationResult, rebuild_relations
from recsys.works import WorksResult, rebuild_works

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


def report(
    results: list[IngestResult],
    links: LinkResult | None,
    pops: PopularityResult | None,
    works: WorksResult | None = None,
    rels: RelationResult | None = None,
) -> None:
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

    if links is not None:
        print("\nEntity resolution")
        print(f"  {links.n_works} works span {links.n_linked} rows; "
              f"{links.n_collapsed} duplicate rows hidden from listings")

    if pops is not None and pops.scales:
        print("\nPopularity calibration")
        print(table(
            ["domain", "source", "method", "rows", "anchors", "knots", "scale"],
            [[
                s.domain, s.source, s.method, str(s.n_rows), str(s.n_anchors),
                str(len(s.knots)), f"{s.offset:+.3f}",
            ] for s in pops.scales],
        ))
        for s in pops.fallbacks:
            print(f"\n  !! {s.domain}/{s.source}: no shared works to calibrate on; "
                  f"quantile-mapped, which assumes its rows are representative")

    if works is not None:
        print("\nRead view")
        print(f"  {works.n_works} works indexed for reading; "
              f"{works.n_uncalibrated} ordered by a raw count")

    if rels is not None:
        print("\nRelation edges")
        top = sorted(rels.by_relation.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
        print(f"  {rels.n_edges} edges: " + ", ".join(f"{n} {r}" for r, n in top))

    with session() as conn:
        total = count_items(conn)
        rows = count_rows(conn)
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

    dedup = f"  ({rows} rows before dedup)" if rows != total else ""
    print(f"\n  total items : {total}{dedup}")
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
    parser.add_argument("--no-link", action="store_true",
                        help="skip entity resolution, leaving duplicates visible")
    parser.add_argument("--prune", action="store_true",
                        help="DELETE stored rows that today's ingest filters would "
                             "reject (too few ratings, adult, not a work)")
    parser.add_argument("--prune-dry-run", action="store_true",
                        help="report what --prune would delete, and delete nothing")
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

    if args.prune or args.prune_dry_run:
        # Before linking, so the derived tables are rebuilt over what survives.
        with session() as conn:
            pruned = prune(conn, settings, dry_run=not args.prune)
        print("\nPrune" + (" (dry run)" if pruned.dry_run else ""))
        for reason, n in pruned.counts.items():
            print(f"  {n:>7}  {reason}")
        verb = "would be removed" if pruned.dry_run else "removed"
        print(f"  {pruned.total:>7}  total flagged; {pruned.deleted} {verb}")

    # Always after ingest, never as a registry entry: resolution has to see
    # every source's rows, and a registry entry would let INGESTERS order it
    # first. Cheap and idempotent, so it runs even on a --no-ingest pass.
    links: LinkResult | None = None
    pops: PopularityResult | None = None
    works: WorksResult | None = None
    rels: RelationResult | None = None
    if not args.no_link:
        with session() as conn:
            links = rebuild_links(conn)
            # Strictly after linking: the calibration anchors are the works
            # that linking just proved two sources both carry.
            pops = rebuild_popularity(conn)
            # Strictly after both: the read view stores what they decided --
            # which row represents a work, and what it sorts by.
            works = rebuild_works(conn)
            # Independent of the three above; it reads only stored raw_json.
            rels = rebuild_relations(conn)

    report(results, links, pops, works, rels)
    return 1 if any(r.status != "ok" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
