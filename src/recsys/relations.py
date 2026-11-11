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

Rebuilt wholesale like every other derived table. The ``json_each`` walk over
every AniList row costs about a second on an 84k-work catalog; doing it once at
rebuild rather than on every read is the whole point.
"""

from __future__ import annotations

import sqlite3
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

#: AniList's media types, mapped to the domains Loom files them under. A type
#: absent here is skipped rather than guessed at.
_TYPE_DOMAIN = {"ANIME": "anime", "MANGA": "manga"}


@dataclass
class RelationResult:
    n_edges: int = 0
    by_relation: dict[str, int] = field(default_factory=dict)


def rebuild_relations(conn: sqlite3.Connection) -> RelationResult:
    """Recompute every relation edge from stored ``raw_json``. Idempotent.

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
        "relations: %d edges (%d adaptation, %d franchise)",
        result.n_edges,
        sum(n for r, n in result.by_relation.items() if r in ADAPTATION_RELATIONS),
        sum(n for r, n in result.by_relation.items() if r in FRANCHISE_RELATIONS),
    )
    return result


#: What a recommendation holds back: a work's own franchise *and* the same
#: story in another medium. The two sets above stay separate because they mean
#: different things to the model -- an adaptation edge is the ground truth
#: ``models/evaluate.py`` scores against, a sequel edge is not -- but to someone
#: asking what to try next they are one answer: "more of what you just rated".
#:
#: ``CHARACTER`` and ``OTHER`` are left out on a measurement, not a hunch.
#: Walking these edges transitively over the 57k-edge catalog, the largest
#: connected group is 330 works (Gundam, which is right). Adding ``OTHER`` grows
#: it to 414 by welding Gridman onto Gundam; adding ``CHARACTER`` produces one
#: group of 4,599 -- every crossover special joins two franchises, and rating
#: one anime would then hide a quarter of the anime catalog.
SUPPRESSED_RELATIONS: frozenset[str] = FRANCHISE_RELATIONS | ADAPTATION_RELATIONS

#: Relations whose *target* is derived from its source: the edge points down
#: from a work to something made out of it. ``PARENT`` is the same statement
#: from the other end.
_DERIVED_RELATIONS: frozenset[str] = frozenset({"SIDE_STORY", "SPIN_OFF", "SUMMARY"})


def franchise_representatives(
    conn: sqlite3.Connection,
    item_uid: str,
    relations: frozenset[str] = SUPPRESSED_RELATIONS,
) -> set[str]:
    """Representative uids of every work in ``item_uid``'s franchise.

    What ``routes.similar`` suppresses. **Transitive**: AniList records a
    relation between neighbours only, so a third season is an edge from the
    second and nothing from the first. One hop found *DEATH NOTE Rewrite* from
    *DEATH NOTE* and none of the manga's own spin-offs, which sit two edges
    away through the manga.

    **But never through a crossover.** Followed without limit, the edges chain
    franchises that have nothing to do with each other: *Dragon Ball Z* reaches
    *ONE PIECE* in four hops through a festival short filed under both, and
    *Nanatsu no Taizai* reaches *FAIRY TAIL* through a joint one-shot. Hiding
    One Piece from someone who rated Dragon Ball is the opposite of the job.
    What those bridges have in common is that they are derived from *two*
    works, so a work with two parents is reached and not walked through: it
    belongs to both franchises, and joins neither to the other. (Unless both
    parents turn out to be in *this* franchise -- an OVA set between two
    seasons has two parents too, and is no crossover.) Measured over
    the 1,500 most popular anime that leaves a median franchise of 5 works and
    a largest of 307 (Gundam), with all of the pairs above apart. One kind of
    bridge survives it -- an anime adapting two unrelated manga, which is how
    *Hanma Baki* still reaches *Kengan Ashura* -- because a manga with many
    adaptations is the normal case and cannot be told from it by edge type.

    It translates on *both* ends, which ``related_uids`` alone does not, and
    that translation is the whole point:

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

    own = set(work_members(conn, item_uid))
    seen = set(own)
    frontier = list(own)
    held: dict[str, set[str]] = {}  # two-parent works reached, not yet walked through
    cleared: set[str] = set()  # ...and the ones since found to be no crossover
    while frontier:
        node = frontier.pop()
        # The query itself is always walked from: rating a crossover does mean
        # you know both of the things it crosses.
        derived_from = set() if node in own or node in cleared else _parents(conn, node)
        if len(derived_from) >= 2:
            held[node] = derived_from
        else:
            reached = related_uids(conn, node, relations) - seen
            seen |= reached
            frontier.extend(reached)
        if not frontier:
            # Two parents is also what an OVA set between two seasons looks
            # like. Once the walk has run out, any held work whose parents all
            # turned out to be in this franchise was that, not a crossover, and
            # is walked through after all. Decided here rather than on arrival
            # so the answer does not depend on which edge was followed first.
            frontier = [n for n, parents in held.items() if parents <= seen]
            for released in frontier:
                del held[released]
                cleared.add(released)
    # Edges run both ways, so the walk always arrives back at the query; a
    # work is not a member of its own franchise for this purpose.
    return {representative_uid(conn, uid) for uid in seen - own} - {
        representative_uid(conn, item_uid)
    }


def _parents(conn: sqlite3.Connection, item_uid: str) -> set[str]:
    """The distinct works ``item_uid`` is derived from.

    Gathered from both ends, because AniList often records only one: the
    special names its ``PARENT``, or the series names its ``SIDE_STORY``.
    """
    marks = ",".join("?" * len(_DERIVED_RELATIONS))
    return {
        row[0]
        for row in conn.execute(
            f"""SELECT dst_item_uid FROM relations
                 WHERE src_item_uid = ? AND relation = 'PARENT'
                UNION
                SELECT src_item_uid FROM relations
                 WHERE dst_item_uid = ? AND relation IN ({marks})""",
            [item_uid, item_uid, *sorted(_DERIVED_RELATIONS)],
        )
    }


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
