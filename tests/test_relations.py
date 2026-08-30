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
    """A relation cannot be both a cross-domain bridge and a thing to suppress."""
    assert not (ADAPTATION_RELATIONS & FRANCHISE_RELATIONS)
