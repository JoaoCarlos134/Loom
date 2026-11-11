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

**It also holds what a listing filters on.** A work's year and score ride on
the row, and its tags sit beside it in ``work_tags`` (with ``tag_counts`` as the
picker's vocabulary). Same argument, different symptom: tags are JSON text on
``items``, and finding the works carrying one meant a ``json_each`` walk of the
whole catalog -- 265 ms a query, measured, against an index probe.

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


#: One scalar from another row of the same work, for a representative that
#: lacks it. NULL for a work of one, whose `l.work_uid` is NULL.
_SIBLING = """(SELECT s.{field} FROM links t JOIN items s ON s.item_uid = t.item_uid
                WHERE t.work_uid = l.work_uid AND s.{field} IS NOT NULL
                ORDER BY s.item_uid LIMIT 1)"""


@dataclass
class WorksResult:
    n_works: int = 0
    n_uncalibrated: int = 0  # works ordered by a raw count, for want of anything better


def _weigh_ratings(conn: sqlite3.Connection) -> None:
    """Fill ``rating_key``: each work's score, discounted by how few gave it.

    A mean is only as good as its count. Sorted by ``mean_rating`` alone the
    catalog's best work was a road-safety short with one vote of 10. The
    standard correction is the one film sites use for their top lists::

        key = (n * R + m * C) / (n + m)

    ``R`` and ``n`` are the work's mean and vote count, ``C`` the mean across
    its domain, and ``m`` how many votes it takes to be believed over ``C``.

    **``m`` is the domain's median vote count, measured at each rebuild.** No
    constant could serve: half the rated TV shows have under 77 votes and half
    the rated books over 21,157, so any fixed ``m`` either ignores every show
    or believes every book. The median says "as well attested as the typical
    work beside it", which is the same claim in every domain.

    Per domain, and so are the keys: an 8.0 here and an 8.0 there were never
    the same statement, and this does not pretend to make them one.
    """
    rated: dict[str, list[tuple[int | None, float]]] = {}
    for row in conn.execute(
        "SELECT domain, n_ratings, mean_rating FROM works WHERE mean_rating IS NOT NULL"
    ):
        rated.setdefault(row["domain"], []).append((row["n_ratings"], row["mean_rating"]))

    for domain, rows in rated.items():
        # The median of the counts that exist. A missing count is not a count
        # of zero: enough of those would drag the median to nothing and let the
        # one-vote ten back in. At least one vote, so the key is defined even
        # where a source reports no counts at all.
        counts = sorted(n for n, _ in rows if n is not None)
        m = max(1, counts[len(counts) // 2]) if counts else 1
        c = sum(r for _, r in rows) / len(rows)
        # A score with no count is taken at the median -- as believable as the
        # typical work beside it, which is the only neutral thing to assume.
        # Taken at zero it would collapse onto the domain mean and every such
        # work would tie, whatever it scored.
        conn.execute(
            """UPDATE works
                  SET rating_key = (COALESCE(n_ratings, ?) * mean_rating + ? * ?)
                                   / (COALESCE(n_ratings, ?) + ?)
                WHERE domain = ? AND mean_rating IS NOT NULL""",
            (m, m, c, m, m, domain),
        )


def rebuild_works(conn: sqlite3.Connection) -> WorksResult:
    """Recompute the read view from scratch. Idempotent.

    Must run *after* ``link.rebuild_links`` and ``popularity.rebuild_popularity``
    -- it is derived from both, and running it first would freeze yesterday's
    ordering into today's catalog.
    """
    with conn:  # one transaction: never leave reads pointing at half a catalog
        conn.execute("DELETE FROM works")
        # Year and score are what a listing filters and sorts on, so they are
        # stored the way a reader sees them: the representative's own value,
        # or failing that a sibling's -- the first in uid order, which is the
        # row `db.fill_from_siblings` fills the displayed item from. A filter
        # that disagreed with the card it produced would be worse than none.
        conn.execute(
            f"""INSERT INTO works (item_uid, domain, title, sort_key,
                                   year, mean_rating, n_ratings)
               SELECT i.item_uid, i.domain, i.title,
                      COALESCE(pn.value, i.popularity),
                      COALESCE(i.year, {_SIBLING.format(field="year")}),
                      COALESCE(i.mean_rating, {_SIBLING.format(field="mean_rating")}),
                      COALESCE(i.n_ratings, {_SIBLING.format(field="n_ratings")})
                 FROM items i
                 LEFT JOIN links l ON l.item_uid = i.item_uid
                 LEFT JOIN popularity_norm pn ON pn.item_uid = i.item_uid
                WHERE (l.item_uid IS NULL OR l.is_primary = 1)"""
        )
        _weigh_ratings(conn)
        # Tags are the representative's own and nothing more: lists are not
        # merged across a work's rows anywhere on the read path, and a filter
        # must not find a work by a tag its card does not show.
        conn.execute("DELETE FROM work_tags")
        conn.execute(
            """INSERT OR IGNORE INTO work_tags (tag, item_uid)
               SELECT t.value, w.item_uid
                 FROM works w
                 JOIN items i ON i.item_uid = w.item_uid,
                      json_each(i.tags) t"""
        )
        conn.execute("DELETE FROM tag_counts")
        conn.execute(
            """INSERT INTO tag_counts (domain, tag, n)
               SELECT w.domain, t.tag, COUNT(*)
                 FROM work_tags t JOIN works w ON w.item_uid = t.item_uid
                GROUP BY w.domain, t.tag"""
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
