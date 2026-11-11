"""Relation edges promoted out of ``raw_json``.

The edges were already being read -- by ``manga_anilist`` to decide what to
fetch, and by ``models/evaluate`` to score against. What these tests pin is
that moving the reading to one rebuild did not change which edges exist, and
that the uid on the far side is spelled the way the rest of the catalog spells
uids. A relation pointing at a uid nobody else writes would join to nothing and
raise nothing.
"""

from __future__ import annotations

import pytest

from recsys.db import connect, init_db, upsert_items
from recsys.relations import (
    ADAPTATION_RELATIONS,
    FRANCHISE_RELATIONS,
    rebuild_relations,
    related_uids,
)
from recsys.schemas import UnifiedItem, make_item_uid, utcnow


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "r.db")
    c = connect(tmp_path / "r.db")
    yield c
    c.close()


def anilist(source_id, title, *, domain="anime", edges=(), source="anilist", raw=True):
    return UnifiedItem(
        domain=domain, source=source, source_id=str(source_id), title=title,
        fetched_at=utcnow(),
        raw_json={"id": source_id, "relations": {"edges": list(edges)}} if raw else None,
    )


def edge(node_id, relation="ADAPTATION", node_type="MANGA"):
    return {"relationType": relation, "node": {"id": node_id, "type": node_type}}


def test_an_edge_becomes_a_row(conn):
    upsert_items(conn, [anilist(1, "Show", edges=[edge(50)])])
    assert rebuild_relations(conn).n_edges == 1


def test_the_target_uid_matches_make_item_uid(conn):
    """Spelled by the helper, never string-formatted at the call site."""
    upsert_items(conn, [anilist(1, "Show", edges=[edge(50)])])
    rebuild_relations(conn)
    stored = conn.execute("SELECT dst_item_uid FROM relations").fetchone()[0]
    assert stored == make_item_uid("manga", "anilist", "50")


def test_an_anime_to_anime_edge_keeps_the_anime_domain(conn):
    upsert_items(conn, [anilist(1, "Show", edges=[edge(7, "SEQUEL", "ANIME")])])
    rebuild_relations(conn)
    stored = conn.execute("SELECT dst_item_uid FROM relations").fetchone()[0]
    assert stored == make_item_uid("anime", "anilist", "7")


def test_an_edge_survives_its_far_side_being_absent(conn):
    """A dangling edge is still a true statement, and dropping it here would
    mean rebuilding every time an ingest supplies the missing side."""
    upsert_items(conn, [anilist(1, "Show", edges=[edge(999)])])
    assert rebuild_relations(conn).n_edges == 1


def test_an_unknown_media_type_is_skipped_rather_than_guessed(conn):
    upsert_items(conn, [anilist(1, "Show", edges=[edge(50, node_type="NOVEL")])])
    assert rebuild_relations(conn).n_edges == 0


def test_a_row_with_no_raw_json_is_not_an_error(conn):
    upsert_items(conn, [anilist(1, "Show", raw=False)])
    assert rebuild_relations(conn).n_edges == 0


def test_a_non_anilist_row_is_ignored(conn):
    """Only AniList states relations; another source's raw_json means something
    else entirely under the same key."""
    upsert_items(conn, [anilist(1, "Show", edges=[edge(50)], source="jikan")])
    assert rebuild_relations(conn).n_edges == 0


def test_a_duplicated_edge_lands_once(conn):
    upsert_items(conn, [anilist(1, "Show", edges=[edge(50), edge(50)])])
    assert rebuild_relations(conn).n_edges == 1


def test_two_relations_between_the_same_pair_are_both_kept(conn):
    upsert_items(conn, [anilist(1, "Show", edges=[edge(50, "ADAPTATION"), edge(50, "SOURCE")])])
    assert rebuild_relations(conn).n_edges == 2


def test_rebuilding_twice_changes_nothing(conn):
    upsert_items(conn, [anilist(1, "Show", edges=[edge(50), edge(51, "SEQUEL", "ANIME")])])
    first = rebuild_relations(conn).n_edges
    assert rebuild_relations(conn).n_edges == first


def test_related_uids_reads_both_directions(conn):
    """AniList records a relation from one side only -- the manga says nothing
    back -- but a caller asking for a franchise means it either way round."""
    upsert_items(conn, [anilist(1, "Show", edges=[edge(2, "SEQUEL", "ANIME")])])
    rebuild_relations(conn)
    a, b = make_item_uid("anime", "anilist", "1"), make_item_uid("anime", "anilist", "2")
    assert related_uids(conn, a) == {b}
    assert related_uids(conn, b) == {a}


def test_related_uids_filters_by_relation(conn):
    upsert_items(conn, [
        anilist(1, "Show", edges=[edge(50, "ADAPTATION"), edge(2, "SEQUEL", "ANIME")]),
    ])
    rebuild_relations(conn)
    src = make_item_uid("anime", "anilist", "1")
    assert related_uids(conn, src, FRANCHISE_RELATIONS) == {
        make_item_uid("anime", "anilist", "2")
    }
    assert related_uids(conn, src, ADAPTATION_RELATIONS) == {
        make_item_uid("manga", "anilist", "50")
    }


def test_the_two_relation_sets_do_not_overlap(conn):
    """A relation is a cross-domain bridge or a within-franchise edge, not both.

    The model is scored against the first kind and not the second, which is why
    they stay two sets -- even though a recommendation holds back both (see
    ``SUPPRESSED_RELATIONS``)."""
    assert not (ADAPTATION_RELATIONS & FRANCHISE_RELATIONS)


# --------------------------------------------------------------------------
# franchise_representatives: suppression is uid-space correct
# --------------------------------------------------------------------------
#
# Regression for a latent bug: relation edges live in AniList-uid space, but the
# neighbours franchise suppression filters are in representative-uid space. When
# a work's AniList row loses the primary election, those two spaces diverge and
# related_uids -- which does not translate -- returns nothing, leaking a sequel
# into "similar". franchise_representatives translates both ends.


def _linked_pair_where_mal_wins_primary(conn):
    """One anime work carried by two rows, where the MAL row represents it.

    The AniList row carries a SEQUEL edge but a null synopsis; the MAL row has a
    description, so _richness hands it the primary election. Returns the two
    uids plus the sequel's representative uid.
    """
    from recsys.link import rebuild_links

    anilist_row = UnifiedItem(
        domain="anime", source="anilist", source_id="100", title="Show",
        description=None,                       # no synopsis -> loses primary
        external_ids={"mal_id": "100"},         # links to the MAL row
        raw_json={"id": 100, "relations": {"edges": [edge(200, "SEQUEL", "ANIME")]}},
        fetched_at=utcnow(),
    )
    mal_row = UnifiedItem(
        domain="anime", source="mal", source_id="100", title="Show",
        description="a real description, so this row wins the primary election",
        external_ids={"mal_id": "100"},
        fetched_at=utcnow(),
    )
    sequel = anilist(200, "Show 2", edges=[])   # the franchise entry, stands alone
    upsert_items(conn, [anilist_row, mal_row, sequel])
    rebuild_links(conn)
    rebuild_relations(conn)
    return (
        make_item_uid("anime", "mal", "100"),       # the work's representative
        make_item_uid("anime", "anilist", "100"),   # the row that carries the edge
        make_item_uid("anime", "anilist", "200"),   # the sequel (its own representative)
    )


def test_franchise_representatives_finds_edges_on_a_non_primary_row(conn):
    rep, _edge_row, sequel = _linked_pair_where_mal_wins_primary(conn)

    # The representative carries no edge of its own...
    from recsys.relations import FRANCHISE_RELATIONS
    assert related_uids(conn, rep, FRANCHISE_RELATIONS) == set()
    # ...yet the franchise is still found, via the work's AniList row.
    from recsys.relations import franchise_representatives
    assert franchise_representatives(conn, rep) == {sequel}


def test_franchise_representatives_returns_the_neighbour_uid_space(conn):
    """The suppression set must be spelled the way neighbours are: representatives."""
    from recsys.link import rebuild_links
    from recsys.relations import franchise_representatives

    # Query work: single AniList row with a SEQUEL edge -> it is its own rep.
    # Sequel work: an AniList row that LOSES primary to a richer MAL sibling,
    # so the sequel's representative is the MAL uid, not the edge's AniList uid.
    query = anilist(1, "A", edges=[edge(2, "SEQUEL", "ANIME")])
    seq_anilist = UnifiedItem(
        domain="anime", source="anilist", source_id="2", title="B",
        description=None, external_ids={"mal_id": "2"}, fetched_at=utcnow(),
        raw_json={"id": 2, "relations": {"edges": []}},
    )
    seq_mal = UnifiedItem(
        domain="anime", source="mal", source_id="2", title="B",
        description="prose, so the MAL row represents work B",
        external_ids={"mal_id": "2"}, fetched_at=utcnow(),
    )
    upsert_items(conn, [query, seq_anilist, seq_mal])
    rebuild_links(conn)
    rebuild_relations(conn)

    got = franchise_representatives(conn, make_item_uid("anime", "anilist", "1"))
    # Resolved to the sequel's representative (MAL), not the edge's AniList uid.
    assert got == {make_item_uid("anime", "mal", "2")}


def test_franchise_representatives_is_empty_for_a_franchiseless_work(conn):
    from recsys.relations import franchise_representatives

    upsert_items(conn, [anilist(1, "Solo", edges=[])])
    rebuild_relations(conn)
    assert franchise_representatives(conn, make_item_uid("anime", "anilist", "1")) == set()
