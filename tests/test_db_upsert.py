"""SQLite layer: migrations, upsert accounting, FTS sync and query shaping.

The FTS index is external-content, so it holds no copy of the text and stays
correct only as long as three triggers fire. Those triggers are the thing most
likely to rot silently, so they get explicit coverage.
"""

from __future__ import annotations

import sqlite3

import pytest

from recsys.db import (
    SCHEMA_VERSION,
    build_fts_query,
    connect,
    count_items,
    domain_stats,
    finish_ingest_run,
    get_item,
    init_db,
    last_ingest_at,
    query_items,
    start_ingest_run,
    upsert_items,
)
from recsys.schemas import Creator, UnifiedItem


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


def item(source_id="1", *, title="Cowboy Bebop", domain="anime", **kw) -> UnifiedItem:
    return UnifiedItem(
        domain=domain, source="jikan", source_id=source_id, title=title, **kw
    )


# ---- migrations ----------------------------------------------------------


def test_init_db_sets_user_version(db_path):
    assert init_db(db_path) == SCHEMA_VERSION


def test_init_db_is_idempotent(db_path):
    assert init_db(db_path) == SCHEMA_VERSION
    assert init_db(db_path) == SCHEMA_VERSION


def test_expected_tables_exist(conn):
    names = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"items", "items_fts", "ingest_runs"} <= names


def test_unique_constraint_on_domain_source_id(conn):
    upsert_items(conn, [item("1")])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO items (item_uid, domain, source, source_id, title, fetched_at)"
            " VALUES ('other:uid', 'anime', 'jikan', '1', 'Dupe', '2024-01-01')"
        )


# ---- upsert accounting ---------------------------------------------------


def test_insert_then_update_counts(conn):
    assert upsert_items(conn, [item("1"), item("2")]) == (2, 0)
    assert upsert_items(conn, [item("1"), item("2")]) == (0, 2)
    assert count_items(conn) == 2


def test_mixed_batch_splits_counts(conn):
    upsert_items(conn, [item("1")])
    assert upsert_items(conn, [item("1"), item("2")]) == (1, 1)


def test_empty_batch_is_a_noop(conn):
    assert upsert_items(conn, []) == (0, 0)
    assert count_items(conn) == 0


def test_within_batch_dedup(conn):
    """A paginated API can hand back the same item twice mid-ranking-shift."""
    n_ins, n_upd = upsert_items(conn, [item("1", title="First"), item("1", title="Second")])
    assert (n_ins, n_upd) == (1, 0)
    assert count_items(conn) == 1
    # Last write in the batch wins.
    assert get_item(conn, "anime:jikan:1").title == "Second"


def test_upsert_overwrites_scalar_fields(conn):
    upsert_items(conn, [item("1", mean_rating=8.75, year=1998)])
    upsert_items(conn, [item("1", mean_rating=9.0, year=1999)])
    got = get_item(conn, "anime:jikan:1")
    assert (got.mean_rating, got.year) == (9.0, 1999)


def test_upsert_preserves_nested_structures(conn):
    upsert_items(
        conn,
        [item("1", creators=[Creator(name="Sunrise", role="studio")], tags=["Action"])],
    )
    got = get_item(conn, "anime:jikan:1")
    assert got.creators == [Creator(name="Sunrise", role="studio")]
    assert got.tags == ["action"]


def test_batch_larger_than_the_lookup_chunk(conn):
    """upsert_items chunks its existence lookup at 500 ids."""
    batch = [item(str(i), title=f"Show {i}") for i in range(1200)]
    assert upsert_items(conn, batch) == (1200, 0)
    assert upsert_items(conn, batch) == (0, 1200)


# ---- FTS sync ------------------------------------------------------------


def test_fts_finds_an_inserted_item(conn):
    upsert_items(conn, [item("1", title="Fullmetal Alchemist: Brotherhood")])
    total, items = query_items(conn, q="alchemist")
    assert total == 1 and items[0].source_id == "1"


def test_fts_stays_in_sync_after_update(conn):
    """The AFTER UPDATE trigger must delete the old index row, not just add one."""
    upsert_items(conn, [item("1", title="Original Title")])
    assert query_items(conn, q="Original")[0] == 1

    upsert_items(conn, [item("1", title="Changed Title")])
    assert query_items(conn, q="Original")[0] == 0
    assert query_items(conn, q="Changed")[0] == 1


def test_fts_stays_in_sync_after_delete(conn):
    upsert_items(conn, [item("1", title="Ephemeral")])
    with conn:
        conn.execute("DELETE FROM items WHERE item_uid = 'anime:jikan:1'")
    assert query_items(conn, q="Ephemeral")[0] == 0


def test_fts_searches_description_and_alt_titles(conn):
    upsert_items(
        conn,
        [item("1", title="Bebop", alt_titles=["Kaubōi Bibappu"], description="A bounty hunter crew.")],
    )
    assert query_items(conn, q="bounty")[0] == 1
    assert query_items(conn, q="Bibappu")[0] == 1


def test_fts_ranks_title_matches_above_description_matches(conn):
    upsert_items(
        conn,
        [
            item("1", title="Unrelated Show", description="a story about alchemy"),
            item("2", title="Alchemy Chronicles", description="unrelated text"),
        ],
    )
    _, items = query_items(conn, q="alchemy")
    assert items[0].source_id == "2"  # title weighted 10x over description


def test_fts_prefix_match_on_last_token(conn):
    upsert_items(conn, [item("1", title="Brotherhood")])
    assert query_items(conn, q="brother")[0] == 1


# ---- FTS query sanitising ------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("bebop", '"bebop"*'),
        ("cowboy bebop", '"cowboy" "bebop"*'),
        ("  spaced   out  ", '"spaced" "out"*'),
    ],
)
def test_build_fts_query_quotes_every_token(raw, expected):
    assert build_fts_query(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "!!!", "***", "-", None])
def test_unsearchable_input_yields_no_query(raw):
    assert build_fts_query(raw) is None


def test_unsearchable_query_returns_empty_without_touching_fts(conn):
    upsert_items(conn, [item("1")])
    assert query_items(conn, q="!!!") == (0, [])


@pytest.mark.parametrize("raw", ['AND', 'NEAR', 'OR', '"', 'a AND b', 'x*', 'a NEAR/2 b', "^start"])
def test_fts_operators_typed_by_a_user_are_treated_as_words(conn, raw):
    """User input must never reach MATCH as syntax -- an unquoted operator raises."""
    upsert_items(conn, [item("1", title="Cowboy Bebop")])
    total, _ = query_items(conn, q=raw)  # must not raise sqlite3.OperationalError
    assert total == 0


def test_quote_injection_does_not_break_out(conn):
    upsert_items(conn, [item("1", title="Cowboy Bebop")])
    total, _ = query_items(conn, q='bebop" OR "')
    assert total >= 0  # the point is that it did not raise


# ---- query shaping -------------------------------------------------------


def test_domain_filter(conn):
    upsert_items(conn, [item("1", domain="anime"), item("2", domain="book")])
    assert query_items(conn, domain="anime")[0] == 1
    assert query_items(conn, domain="book")[0] == 1
    assert query_items(conn)[0] == 2


def test_domain_filter_combines_with_search(conn):
    upsert_items(
        conn,
        [item("1", domain="anime", title="Steins;Gate"), item("2", domain="book", title="Steins Gate Novel")],
    )
    assert query_items(conn, q="steins", domain="anime")[0] == 1


def test_default_ordering_is_popularity_desc(conn):
    upsert_items(
        conn,
        [
            item("1", title="Small", popularity=10.0),
            item("2", title="Huge", popularity=9000.0),
            item("3", title="Medium", popularity=500.0),
        ],
    )
    _, items = query_items(conn)
    assert [i.title for i in items] == ["Huge", "Medium", "Small"]


def test_pagination_returns_total_not_page_size(conn):
    upsert_items(conn, [item(str(i), popularity=float(i)) for i in range(10)])
    total, items = query_items(conn, limit=3, offset=0)
    assert total == 10 and len(items) == 3

    _, page2 = query_items(conn, limit=3, offset=3)
    assert {i.item_uid for i in items}.isdisjoint({i.item_uid for i in page2})


def test_offset_past_the_end_is_empty(conn):
    upsert_items(conn, [item("1")])
    total, items = query_items(conn, offset=500)
    assert total == 1 and items == []


def test_query_results_exclude_raw_json(conn):
    """raw_json is a debugging aid, not payload -- it must not reach the API."""
    upsert_items(conn, [item("1", raw_json={"big": "blob"})])
    _, items = query_items(conn)
    assert items[0].raw_json is None


def test_get_item_returns_none_for_unknown_uid(conn):
    assert get_item(conn, "anime:jikan:nope") is None


# ---- ingest_runs bookkeeping ---------------------------------------------


def test_run_lifecycle_recorded(conn):
    run_id = start_ingest_run(conn, "jikan", "anime")
    row = conn.execute("SELECT * FROM ingest_runs WHERE id=?", (run_id,)).fetchone()
    assert row["status"] == "running" and row["finished_at"] is None

    finish_ingest_run(conn, run_id, n_fetched=3, n_inserted=3, status="ok")
    row = conn.execute("SELECT * FROM ingest_runs WHERE id=?", (run_id,)).fetchone()
    assert row["status"] == "ok"
    assert (row["n_fetched"], row["n_inserted"]) == (3, 3)
    assert row["finished_at"] is not None


def test_last_ingest_at_ignores_failed_runs(conn):
    failed = start_ingest_run(conn, "jikan", "anime")
    finish_ingest_run(conn, failed, status="error", error="HTTP 504")
    assert last_ingest_at(conn) is None

    ok = start_ingest_run(conn, "jikan", "anime")
    finish_ingest_run(conn, ok, status="ok")
    assert last_ingest_at(conn) is not None


def test_domain_stats_groups_and_counts(conn):
    upsert_items(
        conn,
        [item("1", domain="anime"), item("2", domain="anime"), item("3", domain="book")],
    )
    run = start_ingest_run(conn, "jikan", "anime")
    finish_ingest_run(conn, run, status="ok")

    stats = {s["domain"]: s for s in domain_stats(conn)}
    assert stats["anime"]["n_items"] == 2
    assert stats["book"]["n_items"] == 1
    assert stats["anime"]["sources"] == ["jikan"]
    assert stats["anime"]["last_ingest_at"] is not None
    assert stats["book"]["last_ingest_at"] is None


def test_domain_stats_empty_on_empty_db(conn):
    assert domain_stats(conn) == []
