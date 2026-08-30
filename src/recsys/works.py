"""The read view: one row per work, with the ordering key on the row.

The third derived table, after ``link.py``'s ``links`` and ``popularity.py``'s
``popularity_norm``, and built from both. It stores nothing new -- every value
here is already in the catalog -- it stores it in the shape reads need.

**Why it exists.** Listings show one row per work ordered by calibrated
popularity, which meant joining ``links`` to hide duplicates and
``popularity_norm`` to order, then sorting on
``COALESCE(pn.value, i.popularity)``. That expression reads a column of a
*joined* table, so no index on ``items`` can satisfy it and SQLite has no
choice but to scan every row and sort the survivors in a temporary b-tree.
Measured on an 84k-work catalog: 267 ms for the first page, of which 213 ms was
the sort. Through this table, with the key stored beside the row and indexed,
the same page is 5.5 ms end to end -- 0.48 ms of SQL, the rest the ``COUNT(*)``
and building fifty pydantic models. An isolated benchmark of the SQL alone
promised 0.23 ms; that number was never what a caller would see, which is why
the one quoted here is measured through ``query_items``.

This is the failure ``CLAUDE.md`` warns about in its own words -- *ordering
happens in SQL, so a gap there cannot be patched afterwards*. The fix is not a
cleverer query; it is putting the sort key somewhere an index can reach.

**It is a cache and is treated as one.** Rebuilt wholesale, never maintained
incrementally, and stale between rebuilds exactly like ``links``. Reads fall
back to the live joins when it is empty, so a catalog that has never been
linked still lists.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from recsys.log import get_logger

log = get_logger(__name__)


@dataclass
class WorksResult:
    n_works: int = 0
    n_uncalibrated: int = 0  # works ordered by a raw count, for want of anything better


def rebuild_works(conn: sqlite3.Connection) -> WorksResult:
    """Recompute the read view from scratch. Idempotent.

    Must run *after* ``link.rebuild_links`` and ``popularity.rebuild_popularity``
    -- it is derived from both, and running it first would freeze yesterday's
    ordering into today's catalog.
    """
    with conn:  # one transaction: never leave reads pointing at half a catalog
        conn.execute("DELETE FROM works")
        conn.execute(
            """INSERT INTO works (item_uid, domain, title, sort_key)
               SELECT i.item_uid, i.domain, i.title,
                      COALESCE(pn.value, i.popularity)
                 FROM items i
                 LEFT JOIN links l ON l.item_uid = i.item_uid
                 LEFT JOIN popularity_norm pn ON pn.item_uid = i.item_uid
                WHERE (l.item_uid IS NULL OR l.is_primary = 1)"""
        )

    result = WorksResult(
        n_works=int(conn.execute("SELECT COUNT(*) FROM works").fetchone()[0]),
        n_uncalibrated=int(
            conn.execute(
                """SELECT COUNT(*) FROM works w
                   LEFT JOIN popularity_norm pn ON pn.item_uid = w.item_uid
                   WHERE pn.item_uid IS NULL"""
            ).fetchone()[0]
        ),
    )
    log.info(
        "works: %d works indexed for reading; %d ordered by a raw count",
        result.n_works, result.n_uncalibrated,
    )
    return result
