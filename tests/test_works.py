"""The materialised read view, and the fallback that makes it optional.

``works`` exists for one reason -- an index that can satisfy the ORDER BY -- so
what these tests pin is not speed but that it returns *the same answers* as the
joins it replaces. A faster query that disagrees with the slow one is not an
optimisation, it is a bug with a stopwatch.
"""

from __future__ import annotations

import pytest

from recsys.db import connect, count_items, domain_stats, init_db, query_items, upsert_items
from recsys.link import rebuild_links
from recsys.popularity import rebuild_popularity
from recsys.schemas import UnifiedItem, utcnow
from recsys.works import rebuild_works


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "w.db")
    c = connect(tmp_path / "w.db")
    yield c
    c.close()


def item(source_id, title, *, domain="anime", source="jikan", popularity=None, **kw):
    return UnifiedItem(
        domain=domain, source=source, source_id=str(source_id), title=title,
        popularity=popularity, fetched_at=utcnow(), **kw,
    )


CATALOG = [
    item(1, "Cowboy Bebop", popularity=300.0),
    item(2, "Steins;Gate", popularity=900.0),
    item(3, "Monster", popularity=100.0),
    item(4, "A Novel", domain="book", source="goodbooks", popularity=500.0),
]


def test_the_view_holds_one_row_per_work(conn):
    upsert_items(conn, CATALOG)
    assert rebuild_works(conn).n_works == 4


def test_a_collapsed_duplicate_is_not_in_the_view(conn):
    """The whole point of reading through it: duplicates are already gone."""
    upsert_items(conn, [
        item(5114, "Fullmetal Alchemist", source="mal", external_ids={"mal_id": "5114"}),
        item(5114, "Fullmetal Alchemist", source="anilist", description="Two brothers.",
             external_ids={"mal_id": "5114"}),
    ])
    rebuild_links(conn)
    assert rebuild_works(conn).n_works == 1


def test_the_view_agrees_with_the_join_it_replaces(conn):
    """Same catalog, both code paths, identical page. This is the real test."""
    upsert_items(conn, CATALOG)
    slow_total, slow_items = query_items(conn)
    rebuild_works(conn)
    fast_total, fast_items = query_items(conn)

    assert (slow_total, [i.item_uid for i in slow_items]) == (
        fast_total, [i.item_uid for i in fast_items]
    )


def test_the_domain_filter_agrees_too(conn):
    upsert_items(conn, CATALOG)
    slow = query_items(conn, domain="book")
    rebuild_works(conn)
    assert slow == query_items(conn, domain="book")


def test_search_agrees_too(conn):
    upsert_items(conn, CATALOG)
    slow = query_items(conn, q="novel")
    rebuild_works(conn)
    assert slow == query_items(conn, q="novel")


def test_reads_work_before_the_view_is_built(conn):
    """A fresh catalog has never been linked. It must still list its items.

    The failure this guards against is an empty shelf on a populated database
    -- the worst possible way for a cache to be missing.
    """
    upsert_items(conn, CATALOG)
    total, items = query_items(conn)
    assert total == 4 and len(items) == 4
    assert count_items(conn) == 4


def test_counts_and_stats_agree_across_both_paths(conn):
    upsert_items(conn, CATALOG)
    slow_count, slow_stats = count_items(conn), domain_stats(conn)
    rebuild_works(conn)
    assert count_items(conn) == slow_count
    assert domain_stats(conn) == slow_stats


def test_ordering_uses_the_calibrated_value(conn):
    """`sort_key` must be popularity_norm's opinion, not the raw count."""
    upsert_items(conn, CATALOG)
    rebuild_links(conn)
    rebuild_popularity(conn)
    rebuild_works(conn)
    for uid, value in conn.execute(
        "SELECT w.item_uid, pn.value FROM works w JOIN popularity_norm pn "
        "ON pn.item_uid = w.item_uid"
    ):
        stored = conn.execute(
            "SELECT sort_key FROM works WHERE item_uid = ?", (uid,)
        ).fetchone()[0]
        assert stored == value


def test_a_work_with_no_popularity_still_appears(conn):
    """NULL sorts last, but it must not vanish."""
    upsert_items(conn, [item(9, "Obscure", popularity=None)])
    rebuild_works(conn)
    total, items = query_items(conn)
    assert total == 1 and items[0].title == "Obscure"


def test_rebuilding_twice_changes_nothing(conn):
    upsert_items(conn, CATALOG)
    first = rebuild_works(conn).n_works
    assert rebuild_works(conn).n_works == first


def test_the_view_drops_rows_that_are_gone(conn):
    """It points at items by uid with no foreign key, so a stale row would
    survive a delete and be served as a phantom."""
    upsert_items(conn, CATALOG)
    rebuild_works(conn)
    with conn:
        conn.execute("DELETE FROM items WHERE item_uid = ?", (CATALOG[0].item_uid,))
    assert rebuild_works(conn).n_works == 3
