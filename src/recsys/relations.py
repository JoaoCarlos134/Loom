"""Relation edges, promoted out of ``raw_json`` into something joinable.

AniList states, for every work it carries, which other works it is an
adaptation of, a sequel to, or a side story of. ``anime_anilist`` kept those
edges in ``raw_json`` so that mining them later would cost no further requests,
and ``manga_anilist`` did exactly that -- reading them back with a ``json_each``
walk to decide which manga to fetch. This module does the reading once and
stores the result.

**These are not ``links``.** ``links`` says "these two rows are the same work".
An adaptation is the opposite claim: the anime and the manga are two different
works that happen to tell one story, and collapsing them would hide one behind
the other. They need their own table for the same reason they need their own
word.

**Two things want them.**

*Across domains*, ``ADAPTATION`` and ``SOURCE`` are a hard bridge -- the one
place the catalog knows an anime and a manga belong together without a model
guessing it. That is what ``models/evaluate.py`` scores against.

*Within* a domain, the franchise edges (``SEQUEL``, ``PREQUEL``, ``SIDE_STORY``
and friends) are the answer to the content model's most obvious failure: the
nearest film to *Steins;Gate* is the *Steins;Gate* film. Correct, and not a
recommendation. ``similar_to`` can drop them because they are recorded here.

*Within a series*, goodbooks-10k files every volume as its own book, so a
manga's eight collected volumes were eight cards. Goodreads writes the series
into the title -- "Death Note, Vol. 5: Whiteout (Death Note, #5)" -- and
``SERIES`` edges are read off that suffix: volumes of one series by one first
author are joined, exactly, with no similarity anywhere. A title with no
"#number" makes no claim, because Goodreads uses the same parentheses for
imprints ("A Little Golden Book").

Rebuilt wholesale like every other derived table. The ``json_each`` walk over
every AniList row costs about a second on an 84k-work catalog; doing it once at
rebuild rather than on every read is the whole point. ``works.py`` turns these
edges into groups, so it is rebuilt after this.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from recsys.log import get_logger
from recsys.schemas import make_item_uid

log = get_logger(__name__)

#: Relation types that mean "the same story in another medium". The narrow set
#: is deliberate: ``CHARACTER`` and ``SPIN_OFF`` are associations, not the same
#: story, and treating them as ground truth would score the model against pairs
#: no reader would call an adaptation.
ADAPTATION_RELATIONS: frozenset[str] = frozenset({"ADAPTATION", "SOURCE"})

#: Relation types that mean "same franchise, different entry". Not a bridge --
#: the thing a recommender should usually suppress, because a viewer looking at
#: a work already knows about its own sequels.
FRANCHISE_RELATIONS: frozenset[str] = frozenset(
    {"SEQUEL", "PREQUEL", "SIDE_STORY", "PARENT", "ALTERNATIVE", "SPIN_OFF", "SUMMARY"}
)

#: Relations whose *target* is derived from its source: the edge points down
#: from a work to something made out of it. ``PARENT`` is the same statement
#: from the other end. A work with two such parents is a crossover candidate
#: (see ``works.rebuild_works``).
DERIVED_RELATIONS: frozenset[str] = frozenset({"SIDE_STORY", "SPIN_OFF", "SUMMARY"})

#: Volumes of one book series, read off the Goodreads title convention.
SERIES_RELATION = "SERIES"

#: Every relation that makes two works one story for a reader: what results
#: suppress for a work you asked about or rated, and collapse to one entry.
GROUP_RELATIONS: frozenset[str] = (
    ADAPTATION_RELATIONS | FRANCHISE_RELATIONS | frozenset({SERIES_RELATION})
)

#: "(Series Name, #3)", "(Series #3)", "(Series, #0.1 - 0.4)", "(Series, #3: Part
#: 2 of 2)" at the end of a title -- Goodreads' own convention. The name stops
#: at the last comma before the "#", so "Anita Blake, Vampire Hunter" keeps its
#: comma; a "#" is required, so "(Disney Princess, 5)" makes no claim.
_SERIES_SUFFIX = re.compile(
    r"\(\s*(?P<name>[^()#]*?[^()#\s,])\s*,?\s*#\s*(?P<pos>\d+(?:\.\d+)?)(?P<rest>[^()]*)\)\s*$"
)

#: "The Walking Dead, Vol. 01: Days Gone Bye", "Scott Pilgrim, Volume 1: ...".
#: Comics and manga often carry no suffix at all, and these are exactly the
#: volumes the series exists to fold. Only tried when there is no suffix.
_VOLUME_TITLE = re.compile(
    r"^(?P<name>\S.*?\S),?\s+(?:Vol\.?|Volume)\s*(?P<pos>\d+(?:\.\d+)?)\b", re.IGNORECASE
)

#: What right after the number marks a collection rather than a volume: a
#: range or a list running on to another number -- "#1-3" (hyphen or en dash),
#: "#1-4 + 3.5", "#1, 2 & 3". A comma followed by words is a second series'
#: name ("#40, Moist von Lipwig #3"), not a collection.
_COLLECTION_TAIL = re.compile(r"^\s*[-\u2013+&,]\s*#?\s*\d")


@dataclass(frozen=True)
class Series:
    name: str
    position: float
    #: A boxed set or omnibus ("#1-3"): one of the series, never its card.
    is_collection: bool = False


def series_of(title: str | None) -> Series | None:
    """The series a Goodreads-style title places a book in, or ``None``."""
    text = title or ""
    match = _SERIES_SUFFIX.search(text)
    if match:
        # "(Discworld, #11; Death, #2)" is two memberships, not a collection;
        # only what follows this series' own number counts.
        rest = match.group("rest").split(";", 1)[0]
        return Series(
            match.group("name").strip(),
            float(match.group("pos")),
            _COLLECTION_TAIL.match(rest) is not None,
        )
    match = _VOLUME_TITLE.match(text)
    if match:
        return Series(match.group("name").strip(), float(match.group("pos")))
    return None


def series_name(title: str | None) -> str | None:
    """The series a Goodreads-style title names, or ``None`` if it names none."""
    found = series_of(title)
    return found.name if found else None


#: AniList's media types, mapped to the domains Loom files them under. A type
#: absent here is skipped rather than guessed at.
_TYPE_DOMAIN = {"ANIME": "anime", "MANGA": "manga"}


@dataclass
class RelationResult:
    n_edges: int = 0
    by_relation: dict[str, int] = field(default_factory=dict)


def rebuild_relations(conn: sqlite3.Connection) -> RelationResult:
    """Recompute every relation edge from stored rows. Idempotent.

    AniList's edges come out of ``raw_json``; ``SERIES`` edges are read off
    book titles (see ``series_of``).

    Independent of ``links`` and ``popularity_norm`` -- it reads only what the
    ingesters stored -- so it can run in any order relative to them.

    Edges are kept even when the far side is not in the catalog. A dangling
    edge is a true statement about the work that costs one row, and pruning it
    here would mean rebuilding relations every time an ingest adds the missing
    side.
    """
    rows = conn.execute(
        """SELECT a.item_uid AS src,
                  json_extract(e.value, '$.node.id')     AS dst_id,
                  json_extract(e.value, '$.node.type')   AS dst_type,
                  json_extract(e.value, '$.relationType') AS relation
             FROM items a,
                  json_each(json_extract(a.raw_json, '$.relations.edges')) e
            WHERE a.source = 'anilist' AND a.raw_json IS NOT NULL"""
    )

    result = RelationResult()
    to_insert: list[tuple[str, str, str]] = []
    for row in rows:
        domain = _TYPE_DOMAIN.get(row["dst_type"] or "")
        if domain is None or row["dst_id"] is None or not row["relation"]:
            continue
        # Through make_item_uid, never string-formatted here: that helper is the
        # only place the uid format is written down, and an edge pointing at a
        # uid spelled differently would join to nothing and error nowhere.
        to_insert.append(
            (row["src"], make_item_uid(domain, "anilist", str(row["dst_id"])), row["relation"])
        )
        result.by_relation[row["relation"]] = result.by_relation.get(row["relation"], 0) + 1

    for src, dst in _series_edges(conn):
        to_insert.append((src, dst, SERIES_RELATION))
        result.by_relation[SERIES_RELATION] = result.by_relation.get(SERIES_RELATION, 0) + 1

    with conn:
        conn.execute("DELETE FROM relations")
        # OR IGNORE because one pair can legitimately appear twice in a source
        # record; the primary key already says an edge is a (src, dst, relation).
        conn.executemany(
            "INSERT OR IGNORE INTO relations (src_item_uid, dst_item_uid, relation) "
            "VALUES (?, ?, ?)",
            to_insert,
        )

    result.n_edges = int(conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0])
    log.info(
        "relations: %d edges (%d adaptation, %d franchise, %d series)",
        result.n_edges,
        sum(n for r, n in result.by_relation.items() if r in ADAPTATION_RELATIONS),
        sum(n for r, n in result.by_relation.items() if r in FRANCHISE_RELATIONS),
        result.by_relation.get(SERIES_RELATION, 0),
    )
    return result


def _series_edges(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Edges joining each book series' volumes to its first-numbered one.

    A star rather than every pair: groups are connected components, so one
    edge per volume says everything and a 41-book series costs 40 rows, not
    820. Keyed by the series name *and* the first credited author, because two
    authors can each write a series called "Legacy".
    """
    series: dict[tuple[str, str], list[tuple[float, str]]] = defaultdict(list)
    for row in conn.execute("SELECT item_uid, title, creators FROM items WHERE domain = 'book'"):
        found = series_of(row["title"])
        if found is None:
            continue
        try:
            creators = json.loads(row["creators"] or "[]")
        except json.JSONDecodeError:
            creators = []
        author = (creators[0].get("name") or "") if creators else ""
        series[(found.name.casefold(), author.casefold())].append(
            (found.position, row["item_uid"])
        )

    edges: list[tuple[str, str]] = []
    for volumes in series.values():
        if len(volumes) < 2:
            continue
        volumes.sort()
        first = volumes[0][1]
        edges.extend((uid, first) for _, uid in volumes[1:])
    return edges


def franchise_representatives(
    conn: sqlite3.Connection,
    item_uid: str,
    relations: frozenset[str] = GROUP_RELATIONS,
) -> set[str]:
    """Representative uids of every work one edge away in ``relations``.

    By default every relation that makes two works one story -- adaptations,
    franchise entries and series volumes (``GROUP_RELATIONS``), not only the
    franchise ones the name dates from. The read path uses it beside the
    transitive groups ``works.py`` stores, so a work the read view has not
    seen yet is still covered one hop out. It translates on *both* ends, which
    ``related_uids`` alone does not, and that translation is the whole point:

    * Edges live on the AniList row (``relations`` keys them ``…:anilist:…``),
      which need not be the row a caller holds -- when a richer non-AniList row
      wins the primary election, the representative is a *different* uid with no
      edges on it. So edges are gathered from every row of the query's own work,
      not just the one passed in.
    * The far side of each edge is likewise an AniList uid, while the neighbours
      being filtered are representative uids. So each related uid is resolved to
      the row that represents *its* work.

    Without this, suppression silently works only when a work's representative
    happens to be its AniList row, and leaks a sequel into "similar" the moment
    it does not -- the uid-space trap the rest of the read path already avoids.
    """
    from recsys.link import representative_uid, work_members

    related: set[str] = set()
    for member in work_members(conn, item_uid):
        related |= related_uids(conn, member, relations)
    return {representative_uid(conn, uid) for uid in related}


def related_uids(
    conn: sqlite3.Connection, item_uid: str, relations: frozenset[str] | None = None
) -> set[str]:
    """Every item ``item_uid`` is joined to by an edge, in either direction.

    Both directions, because AniList records a relation from one side only: an
    anime says it adapts a manga and the manga says nothing back. A caller
    asking "what is this work's franchise" means the franchise either way round.
    """
    sql = (
        "SELECT dst_item_uid AS other FROM relations WHERE src_item_uid = ?"
        " UNION SELECT src_item_uid FROM relations WHERE dst_item_uid = ?"
    )
    params: list[object] = [item_uid, item_uid]
    if relations is not None:
        marks = ",".join("?" * len(relations))
        ordered = sorted(relations)
        sql = (
            f"SELECT dst_item_uid AS other FROM relations "
            f"WHERE src_item_uid = ? AND relation IN ({marks})"
            f" UNION SELECT src_item_uid FROM relations "
            f"WHERE dst_item_uid = ? AND relation IN ({marks})"
        )
        params = [item_uid, *ordered, item_uid, *ordered]
    return {r[0] for r in conn.execute(sql, params)}
