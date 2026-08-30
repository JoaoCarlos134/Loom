"""Entity resolution, and the duplicate collapsing it drives.

Two rules are being pinned here, and they pull against each other on purpose:

* nothing is ever merged or deleted -- every source's row survives, reachable
  by uid, because Phase 3 wants the union of a work's rows;
* a reader never sees the same work twice.
"""

from __future__ import annotations

import pytest

from recsys.db import (
    connect,
    fill_from_siblings,
    count_items,
    count_rows,
    domain_stats,
    get_item,
    init_db,
    query_items,
    upsert_items,
)
from recsys.link import rebuild_links, work_members
from recsys.schemas import UnifiedItem, make_work_uid


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "links.db"
    init_db(path)
    c = connect(path)
    yield c
    c.close()


def item(source, source_id, *, mal_id=None, title="Thing", description=None,
         tags=(), n_ratings=None, popularity=None, domain="anime"):
    return UnifiedItem(
        domain=domain, source=source, source_id=str(source_id), title=title,
        description=description, tags=list(tags), n_ratings=n_ratings,
        popularity=popularity,
        external_ids={"mal_id": str(mal_id)} if mal_id is not None else {},
    )


# ---- grouping -------------------------------------------------------------


def test_rows_sharing_a_mal_id_become_one_work(conn):
    upsert_items(conn, [item("mal", 5114, mal_id=5114), item("anilist", 99, mal_id=5114)])
    result = rebuild_links(conn)
    assert (result.n_works, result.n_linked, result.n_collapsed) == (1, 2, 1)


def test_a_single_source_work_gets_no_link_row(conn):
    """"No link row" is how reads spell "this is the only row for this work"."""
    upsert_items(conn, [item("mal", 1, mal_id=1)])
    assert rebuild_links(conn).n_works == 0
    assert conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 0


def test_rows_without_a_shared_id_are_never_linked(conn):
    """Linking is by exact id only. No fuzzy title matching, no false merges."""
    upsert_items(conn, [
        item("mal", 1, mal_id=1, title="Cowboy Bebop"),
        item("anilist", 2, title="Cowboy Bebop"),  # same title, no mal_id
    ])
    assert rebuild_links(conn).n_works == 0
    assert query_items(conn)[0] == 2


def test_three_sources_collapse_to_one_work(conn):
    upsert_items(conn, [
        item("mal", 5114, mal_id=5114),
        item("jikan", 5114, mal_id=5114),
        item("anilist", 99, mal_id=5114),
    ])
    result = rebuild_links(conn)
    assert (result.n_works, result.n_linked, result.n_collapsed) == (1, 3, 2)
    assert query_items(conn)[0] == 1


def test_the_work_uid_is_derived_from_the_shared_id(conn):
    upsert_items(conn, [item("mal", 5114, mal_id=5114), item("anilist", 99, mal_id=5114)])
    rebuild_links(conn)
    rows = {r["work_uid"] for r in conn.execute("SELECT work_uid FROM links")}
    assert rows == {make_work_uid("anime", "mal_id", "5114")}


def test_the_same_mal_id_in_two_domains_is_two_works(conn):
    """Id spaces are per-domain; anime 1 and book 1 are unrelated."""
    upsert_items(conn, [
        item("mal", 1, mal_id=1), item("anilist", 9, mal_id=1),
        item("mal", 1, mal_id=1, domain="book"), item("anilist", 9, mal_id=1, domain="book"),
    ])
    assert rebuild_links(conn).n_works == 2


# ---- which row wins -------------------------------------------------------


def test_the_row_with_a_synopsis_wins(conn):
    """The reason AniList rows should win, expressed as content, not a name."""
    upsert_items(conn, [
        item("mal", 5114, mal_id=5114, title="From the dump"),
        item("anilist", 99, mal_id=5114, title="From AniList", description="A long synopsis."),
    ])
    rebuild_links(conn)
    _, items = query_items(conn)
    assert [i.title for i in items] == ["From AniList"]


def test_a_longer_blurb_does_not_beat_a_richer_row(conn):
    """Length is not richness. The CSV dump's `background` is production trivia
    and is routinely longer than a real synopsis, so ranking by length handed
    works to the trivia. Tag count decides before length does."""
    upsert_items(conn, [
        item("mal", 6547, mal_id=6547, title="Trivia",
             description="x" * 593, tags=["a", "b", "c", "d", "e", "f"]),
        item("anilist", 99, mal_id=6547, title="Synopsis",
             description="y" * 398, tags=[f"t{n}" for n in range(20)]),
    ])
    rebuild_links(conn)
    assert query_items(conn)[1][0].title == "Synopsis"


def test_richer_tags_break_a_tie_when_neither_has_text(conn):
    upsert_items(conn, [
        item("mal", 5114, mal_id=5114, title="Thin", tags=["action"]),
        item("jikan", 5114, mal_id=5114, title="Rich", tags=["action", "drama", "sci-fi"]),
    ])
    rebuild_links(conn)
    assert query_items(conn)[1][0].title == "Rich"


def test_selection_never_depends_on_the_source_name(conn):
    """A source-preference list would put source names in the resolver and make
    adding a source mean editing it. Content decides instead."""
    upsert_items(conn, [
        item("mal", 5114, mal_id=5114, title="Dump with text", description="Real prose here."),
        item("anilist", 99, mal_id=5114, title="AniList without text"),
    ])
    rebuild_links(conn)
    assert query_items(conn)[1][0].title == "Dump with text"


def test_the_winner_is_stable_across_rebuilds(conn):
    """Identical rows must not reshuffle the catalog on every bootstrap."""
    upsert_items(conn, [item("mal", 1, mal_id=1), item("anilist", 2, mal_id=1)])
    rebuild_links(conn)
    first = query_items(conn)[1][0].item_uid
    for _ in range(3):
        rebuild_links(conn)
        assert query_items(conn)[1][0].item_uid == first


# ---- nothing is destroyed -------------------------------------------------


def test_the_losing_row_still_exists_and_is_reachable(conn):
    upsert_items(conn, [
        item("mal", 5114, mal_id=5114),
        item("anilist", 99, mal_id=5114, description="Synopsis."),
    ])
    rebuild_links(conn)
    assert count_rows(conn) == 2
    assert get_item(conn, "anime:mal:5114") is not None


def test_work_members_returns_every_row_of_a_work(conn):
    """What Phase 3 reads: the union of a work's rows, not just the winner."""
    upsert_items(conn, [
        item("mal", 5114, mal_id=5114),
        item("anilist", 99, mal_id=5114, description="Synopsis."),
    ])
    rebuild_links(conn)
    assert work_members(conn, "anime:mal:5114") == ["anime:anilist:99", "anime:mal:5114"]


def test_work_members_of_an_unlinked_item_is_itself(conn):
    upsert_items(conn, [item("mal", 1, mal_id=1)])
    rebuild_links(conn)
    assert work_members(conn, "anime:mal:1") == ["anime:mal:1"]


# ---- idempotency and rebuilds --------------------------------------------


def test_rebuilding_is_idempotent(conn):
    upsert_items(conn, [item("mal", 1, mal_id=1), item("anilist", 2, mal_id=1)])
    rebuild_links(conn)
    before = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    rebuild_links(conn)
    assert conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == before


def test_a_rebuild_drops_links_whose_rows_are_gone(conn):
    upsert_items(conn, [item("mal", 1, mal_id=1), item("anilist", 2, mal_id=1)])
    rebuild_links(conn)
    conn.execute("DELETE FROM items WHERE item_uid='anime:anilist:2'")
    conn.commit()
    assert rebuild_links(conn).n_works == 0
    assert query_items(conn)[0] == 1


def test_links_survive_a_re_ingest_of_the_same_rows(conn):
    upsert_items(conn, [item("mal", 1, mal_id=1), item("anilist", 2, mal_id=1)])
    rebuild_links(conn)
    upsert_items(conn, [item("mal", 1, mal_id=1)])  # re-ingest updates in place
    assert query_items(conn)[0] == 1


# ---- how reads behave -----------------------------------------------------


def test_search_returns_one_hit_per_work(conn):
    upsert_items(conn, [
        item("mal", 5114, mal_id=5114, title="Fullmetal Alchemist"),
        item("anilist", 99, mal_id=5114, title="Fullmetal Alchemist", description="Alchemy."),
    ])
    rebuild_links(conn)
    total, items = query_items(conn, q="alchemist")
    assert total == 1 and len(items) == 1


def test_counts_report_works_while_rows_reports_storage(conn):
    upsert_items(conn, [item("mal", 1, mal_id=1), item("anilist", 2, mal_id=1)])
    rebuild_links(conn)
    assert (count_items(conn), count_rows(conn)) == (1, 2)


def test_domain_stats_counts_works_but_credits_every_source(conn):
    """A source that supplied data is a source of the catalog even if its rows
    all lost the primary election."""
    upsert_items(conn, [
        item("mal", 1, mal_id=1),
        item("anilist", 2, mal_id=1, description="Synopsis."),
    ])
    rebuild_links(conn)
    stats = domain_stats(conn)[0]
    assert stats["n_items"] == 1
    assert stats["sources"] == ["anilist", "mal"]


def test_the_domain_filter_still_applies_after_collapsing(conn):
    upsert_items(conn, [
        item("mal", 1, mal_id=1), item("anilist", 2, mal_id=1),
        item("mal", 7, mal_id=7, domain="book"),
    ])
    rebuild_links(conn)
    assert query_items(conn, domain="anime")[0] == 1
    assert query_items(conn, domain="book")[0] == 1
    assert query_items(conn)[0] == 2


def test_paging_is_consistent_with_the_collapsed_total(conn):
    upsert_items(conn, [item("mal", i, mal_id=i, popularity=float(i)) for i in range(1, 6)])
    upsert_items(conn, [item("anilist", 100 + i, mal_id=i, description="x") for i in range(1, 6)])
    rebuild_links(conn)
    total, page = query_items(conn, limit=3, offset=0)
    assert total == 5 and len(page) == 3
    assert len({i.item_uid for i in page}) == 3


# ---- no single source is complete -----------------------------------------


def test_a_gap_in_the_winner_is_filled_from_its_sibling(conn):
    """Open Library has a book's synopsis and none of its ratings; goodbooks has
    the ratings and no prose. The row with text wins, so without this a book
    would display with a synopsis and no score -- worse than either row alone."""
    upsert_items(conn, [
        item("goodbooks", 1, mal_id=1, title="Rated", n_ratings=5000, popularity=5000.0),
        item("openlibrary", 2, mal_id=1, title="Described", description="Real prose."),
    ])
    rebuild_links(conn)
    _, items = query_items(conn)
    assert (items[0].title, items[0].description) == ("Described", "Real prose.")
    assert items[0].n_ratings == 5000  # borrowed from the sibling


def test_a_populated_field_is_never_overwritten(conn):
    """Only nulls are filled, so no source's own datum is replaced by another's."""
    upsert_items(conn, [
        # The richer row wins: it has a description and more tags.
        item("winner", 1, mal_id=1, description="Mine", n_ratings=10, tags=["a", "b"]),
        item("loser", 2, mal_id=1, description="Theirs", n_ratings=99),
    ])
    rebuild_links(conn)
    _, items = query_items(conn)
    assert items[0].source == "winner"
    assert items[0].description == "Mine"
    assert items[0].n_ratings == 10  # not the sibling's 99


def test_tags_are_not_merged(conn):
    """Filling a null is unambiguous; merging two sources' tags is a modelling
    decision, and Phase 3 reads the real union through work_members()."""
    upsert_items(conn, [
        item("a", 1, mal_id=1, tags=["alpha"], description="text"),
        item("b", 2, mal_id=1, tags=["beta", "gamma"]),
    ])
    rebuild_links(conn)
    _, items = query_items(conn)
    assert "beta" not in items[0].tags


def test_an_unlinked_item_is_untouched(conn):
    upsert_items(conn, [item("solo", 1, mal_id=1)])
    rebuild_links(conn)
    _, items = query_items(conn)
    assert items[0].description is None


def test_get_item_fills_too(conn):
    upsert_items(conn, [
        item("a", 1, mal_id=1, n_ratings=42),
        item("b", 2, mal_id=1, description="prose"),
    ])
    rebuild_links(conn)
    assert get_item(conn, "anime:b:2").n_ratings == 42


def test_filling_an_empty_page_is_a_noop(conn):
    assert fill_from_siblings(conn, []) == []


def test_reads_are_unaffected_before_any_link_is_built(conn):
    """The links table starts empty; every row must still be visible."""
    upsert_items(conn, [item("mal", 1, mal_id=1), item("anilist", 2, mal_id=1)])
    assert query_items(conn)[0] == 2
