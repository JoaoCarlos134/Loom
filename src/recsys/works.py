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

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from recsys.db import title_key, title_tokens
from recsys.log import get_logger
from recsys.relations import DERIVED_RELATIONS, GROUP_RELATIONS, SERIES_RELATION, series_of

log = get_logger(__name__)


#: One scalar from another row of the same work, for a representative that
#: lacks it. NULL for a work of one, whose `l.work_uid` is NULL.
_SIBLING = """(SELECT s.{field} FROM links t JOIN items s ON s.item_uid = t.item_uid
                WHERE t.work_uid = l.work_uid AND s.{field} IS NOT NULL{scope}
                ORDER BY s.item_uid LIMIT 1)"""

#: A score or a vote count comes only from a row in the work's own domain --
#: an anime's TMDB entry does not count its MAL audience. The same rule as
#: ``db._SAME_DOMAIN_ONLY``, so a filter agrees with the card it produces.
_SAME_DOMAIN = " AND s.domain = i.domain"


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
            f"""SELECT i.item_uid, i.domain, i.title, i.alt_titles,
                      COALESCE(pn.value, i.popularity) AS sort_key,
                      -- Year and score are what a listing filters and sorts
                      -- on, so they are stored the way a reader sees them:
                      -- the representative's own, or failing that a
                      -- sibling's, as `db.fill_from_siblings` fills the card.
                      COALESCE(i.year, {_SIBLING.format(field="year", scope="")}) AS year,
                      COALESCE(i.mean_rating, {_SIBLING.format(field="mean_rating", scope=_SAME_DOMAIN)})
                          AS mean_rating,
                      COALESCE(i.n_ratings, {_SIBLING.format(field="n_ratings", scope=_SAME_DOMAIN)})
                          AS n_ratings
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

    # A work derived from two works -- a festival short filed under both
    # *Dragon Ball Z* and *ONE PIECE*, a joint one-shot -- belongs to each and
    # joins neither to the other. Unioned through, such bridges welded those
    # two (141 works) and *Kaguya-sama* to *[Oshi no Ko]* on the full build,
    # and no component got near `max_group_size`, so the cap never caught it.
    # Its edges are held back, and let in only once all its parents have
    # turned out to be one story anyway (an OVA set between two seasons).
    # AniList often records one end only, so parents come from both.
    parents: dict[str, set[str]] = defaultdict(set)
    for src, dst, relation in edges:
        a, b = rep_of.get(src, src), rep_of.get(dst, dst)
        if relation == "PARENT":
            parents[a].add(b)
        elif relation in DERIVED_RELATIONS:
            parents[b].add(a)
    held = {u for u, ps in parents.items() if len(ps - {u}) >= 2}

    groups, series = _Components(), _Components()
    series_rows: set[str] = set()
    waiting: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for src, dst, relation in edges:
        a, b = rep_of.get(src, src), rep_of.get(dst, dst)
        if a in held or b in held:
            waiting[a if a in held else b].append((a, b))
        else:
            groups.union(a, b)
        if relation == SERIES_RELATION:
            series.union(a, b)
            series_rows.update((src, dst))
    released = True
    while released:  # one release can make another's parents one story
        released = False
        for uid in [u for u in waiting if len({groups.find(p) for p in parents[u] - {u}}) == 1]:
            for a, b in waiting.pop(uid):
                if a not in waiting and b not in waiting:
                    groups.union(a, b)
                else:  # its other end is still held; union once that one is let in
                    waiting[b if a == uid else a].append((a, b))
            released = True

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

    # A cover any row of the work can lend counts: fill_from_siblings shows it.
    with_image = {
        r[0]
        for r in conn.execute(
            """SELECT DISTINCT COALESCE(p.item_uid, i.item_uid) FROM items i
                 LEFT JOIN links l ON l.item_uid = i.item_uid
                 LEFT JOIN links p ON p.work_uid = l.work_uid AND p.is_primary = 1
                WHERE i.image_url IS NOT NULL AND i.image_url != ''"""
        )
    }
    browse_rank = _browse_ranks(works, unlisted, with_image)

    rows = [
        (
            uid, w["domain"], w["title"], w["sort_key"],
            group_of.get(uid), series_uid_of.get(uid), series_name_of.get(uid),
            positions.get(uid), 0 if uid in unlisted else 1, n_series_of.get(uid),
            1 if uid in with_image else 0, browse_rank.get(uid),
            len(title_tokens(w["title"])), title_key(w["title"]), _alt_keys(w["alt_titles"]),
            1 if uid in collections else 0, w["year"], w["mean_rating"], w["n_ratings"],
        )
        for uid, w in works.items()
    ]
    with conn:  # one transaction: never leave reads pointing at half a catalog
        conn.execute("DELETE FROM works")
        conn.executemany(
            """INSERT INTO works (item_uid, domain, title, sort_key, group_uid,
                                  series_uid, series_name, series_pos, listed, n_series,
                                  has_image, browse_rank, title_tokens, title_key,
                                  alt_keys, is_collection, year, mean_rating,
                                  n_ratings)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
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



def _browse_ranks(
    works: dict[str, sqlite3.Row], unlisted: set[str], with_image: set[str]
) -> dict[str, int]:
    """The all-domains listing's order: covers first, domains taking turns.

    Within a domain the calibrated popularity orders works; across domains
    there is no shared scale -- a Deezer fan count and a MAL member count do
    not measure one thing -- so each domain's n-th most popular work sits
    beside every other domain's n-th. A rank, not a percentile: a domain that
    is a top slice of its medium (goodbooks' ten thousand) is not pushed down
    for it. Works without an image follow, interleaved the same way.
    """
    by_domain: dict[str, list[str]] = defaultdict(list)
    for uid, w in works.items():
        if uid not in unlisted:
            by_domain[w["domain"]].append(uid)
    position: dict[str, int] = {}
    for uids in by_domain.values():
        uids.sort(key=lambda u: (works[u]["sort_key"] is None, -(works[u]["sort_key"] or 0.0),
                                 works[u]["title"], u))
        seen_with = seen_without = 0
        for uid in uids:
            if uid in with_image:
                position[uid] = seen_with
                seen_with += 1
            else:
                position[uid] = seen_without
                seen_without += 1
    order = sorted(
        position,
        key=lambda u: (u not in with_image, position[u], works[u]["domain"], u),
    )
    return {uid: n for n, uid in enumerate(order, start=1)}


def _alt_keys(alt_titles: str | None) -> str:
    """Alternative titles as ``|key|key|``, so one ``instr`` finds an exact one."""
    try:
        titles = json.loads(alt_titles or "[]")
    except json.JSONDecodeError:
        titles = []
    keys = [title_key(t) for t in titles if isinstance(t, str)]
    return "|" + "|".join(k for k in keys if k) + "|"
