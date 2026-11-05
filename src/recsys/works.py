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
from collections import defaultdict
from dataclasses import dataclass, field

from recsys.log import get_logger
from recsys.relations import GROUP_RELATIONS, SERIES_RELATION, series_of

log = get_logger(__name__)


@dataclass
class WorksResult:
    n_works: int = 0
    n_uncalibrated: int = 0  # works ordered by a raw count, for want of anything better
    n_groups: int = 0  # groups of 2+ works that results show one of
    n_series: int = 0  # book series of 2+ volumes, each one card
    n_unlisted: int = 0  # volumes folded behind their series' card
    largest_groups: list[tuple[int, str]] = field(default_factory=list)
    #: Components too big to be one story, left ungrouped: (size, a member).
    oversized_groups: list[tuple[int, str]] = field(default_factory=list)


class _Components:
    """Union-find over uids. Small, and the only place it is needed."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def groups(self, members: set[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for uid in self.parent:
            if uid in members:
                out[self.find(uid)].append(uid)
        return {root: sorted(uids) for root, uids in out.items() if len(uids) > 1}


def rebuild_works(
    conn: sqlite3.Connection,
    *,
    max_group_size: int | None = None,
    fold_series: bool | None = None,
) -> WorksResult:
    """Recompute the read view from scratch. Idempotent.

    Must run *after* ``link.rebuild_links``, ``popularity.rebuild_popularity``
    and ``relations.rebuild_relations`` -- it is derived from all three, and
    running it first would freeze yesterday's ordering into today's catalog.

    Besides the sort key, each work gets its **group**: the connected component
    of its adaptation, franchise and series edges, taken in representative
    space. Edges are keyed by the rows that carry them (AniList's, Goodreads'),
    which need not be the rows that represent their works, so both ends are
    translated through ``links`` first. An edge's far side may be outside the
    catalog and still joins the two works that both point at it: two anime
    adapted from one manga are one story whether or not the manga is stored.

    A component larger than ``max_group_size`` (``config.max_group_size`` by
    default) is left ungrouped and reported: past the size of any real
    franchise, a component is crossovers chaining franchises together, and
    treating it as one story would suppress unrelated works wholesale. Its
    members still suppress their direct edges, which the read path adds itself.

    Book volumes additionally get their **series**, and only the series' most
    read volume is listed: one card per series, every volume still reachable
    by search, by uid and from the card's detail. An omnibus or boxed set
    ("#1-3") is never the card. ``fold_series=False`` (``FOLD_BOOK_SERIES``)
    lists every volume again while keeping the series as a group for results.
    """
    if max_group_size is None or fold_series is None:
        from recsys.config import get_settings

        settings = get_settings()
        max_group_size = settings.max_group_size if max_group_size is None else max_group_size
        fold_series = settings.fold_book_series if fold_series is None else fold_series

    works = {
        r["item_uid"]: r
        for r in conn.execute(
            """SELECT i.item_uid, i.domain, i.title,
                      COALESCE(pn.value, i.popularity) AS sort_key
                 FROM items i
                 LEFT JOIN links l ON l.item_uid = i.item_uid
                 LEFT JOIN popularity_norm pn ON pn.item_uid = i.item_uid
                WHERE (l.item_uid IS NULL OR l.is_primary = 1)"""
        )
    }
    rep_of = {
        r["item_uid"]: r["rep"]
        for r in conn.execute(
            """SELECT mine.item_uid, p.item_uid AS rep FROM links mine
                 JOIN links p ON p.work_uid = mine.work_uid AND p.is_primary = 1"""
        )
    }

    marks = ",".join("?" * len(GROUP_RELATIONS))
    edges = conn.execute(
        f"SELECT src_item_uid, dst_item_uid, relation FROM relations "
        f"WHERE relation IN ({marks})",
        sorted(GROUP_RELATIONS),
    ).fetchall()

    groups, series = _Components(), _Components()
    series_rows: set[str] = set()
    for src, dst, relation in edges:
        a, b = rep_of.get(src, src), rep_of.get(dst, dst)
        groups.union(a, b)
        if relation == SERIES_RELATION:
            series.union(a, b)
            series_rows.update((src, dst))

    group_of: dict[str, str] = {}
    oversized: list[tuple[int, str]] = []
    for members in groups.groups(set(works)).values():
        if len(members) > max_group_size:
            oversized.append((len(members), members[0]))
            continue
        for uid in members:
            group_of[uid] = members[0]

    # The series' name and each volume's number live on the Goodreads row,
    # which is not necessarily the one representing the work.
    names: dict[str, str] = {}
    positions: dict[str, float] = {}
    collections: set[str] = set()
    for uid in series_rows:
        row = conn.execute("SELECT title FROM items WHERE item_uid = ?", (uid,)).fetchone()
        found = series_of(row["title"]) if row is not None else None
        if found is None:
            continue
        rep = rep_of.get(uid, uid)
        names.setdefault(rep, found.name)
        positions.setdefault(rep, found.position)
        if found.is_collection:
            collections.add(rep)

    series_uid_of: dict[str, str] = {}
    series_name_of: dict[str, str] = {}
    unlisted: set[str] = set()
    n_series_of: dict[str, int] = {}
    for members in series.groups(set(works)).values():
        # The most read single volume -- never a boxed set, which would stand
        # for the series with a book nobody reads first -- then the lowest
        # number, then the smallest uid, so rebuilds agree.
        head = min(
            members,
            key=lambda u: (
                u in collections,
                -(works[u]["sort_key"] or 0.0),
                positions.get(u, float("inf")),
                u,
            ),
        )
        name = next((names[u] for u in members if u in names), None)
        for uid in members:
            series_uid_of[uid] = members[0]
            if name is not None:
                series_name_of[uid] = name
            if uid != head and fold_series:
                unlisted.add(uid)
        n_series_of[head] = len(members) if fold_series else 0

    rows = [
        (
            uid, w["domain"], w["title"], w["sort_key"],
            group_of.get(uid), series_uid_of.get(uid), series_name_of.get(uid),
            positions.get(uid), 0 if uid in unlisted else 1, n_series_of.get(uid),
        )
        for uid, w in works.items()
    ]
    with conn:  # one transaction: never leave reads pointing at half a catalog
        conn.execute("DELETE FROM works")
        conn.executemany(
            """INSERT INTO works (item_uid, domain, title, sort_key, group_uid,
                                  series_uid, series_name, series_pos, listed, n_series)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    sizes: dict[str, int] = defaultdict(int)
    for g in group_of.values():
        sizes[g] += 1
    result = WorksResult(
        n_works=len(rows),
        n_uncalibrated=int(
            conn.execute(
                """SELECT COUNT(*) FROM works w
                   LEFT JOIN popularity_norm pn ON pn.item_uid = w.item_uid
                   WHERE pn.item_uid IS NULL"""
            ).fetchone()[0]
        ),
        n_groups=len(sizes),
        n_series=len(n_series_of),
        n_unlisted=len(unlisted),
        largest_groups=sorted(((n, g) for g, n in sizes.items()), reverse=True)[:5],
        oversized_groups=sorted(oversized, reverse=True),
    )
    log.info(
        "works: %d works indexed for reading; %d ordered by a raw count; "
        "%d groups, %d series (%d volumes behind their series' card)",
        result.n_works, result.n_uncalibrated, result.n_groups, result.n_series,
        result.n_unlisted,
    )
    return result

