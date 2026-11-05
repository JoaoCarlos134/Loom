"""MAL user-list row -> ``ratings`` row, and the loader around it.

Driven by `tests/fixtures/mal_ratings_sample.csv`: real rows from the Kaggle
dump plus the edge cases that decide what is kept -- an unrated entry
(`my_score` = 0), an empty score, an out-of-range score, a missing username, a
zeroed timestamp and a unix-epoch timestamp.

Eleven rows in; seven survive.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from recsys.config import Settings, get_settings
from recsys.db import connect, init_db, upsert_ratings
from recsys.ingest.ratings_mal_csv import (
    MalRatingsLoader,
    _parse_rated_at,
    map_rating,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mal_ratings_sample.csv"

N_ROWS = 11
N_KEPT = 7
N_SKIPPED = N_ROWS - N_KEPT


@pytest.fixture
def rows() -> list[dict]:
    with FIXTURE.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "ratings.db"
    init_db(path)
    c = connect(path)
    yield c
    c.close()


@pytest.fixture
def settings(monkeypatch) -> Settings:
    monkeypatch.setenv("MAL_RATINGS_PATH", str(FIXTURE))
    get_settings.cache_clear()
    return get_settings()


# ---- mapping --------------------------------------------------------------


def test_maps_a_scored_row():
    row = map_rating("karthiga", "21", "9", "2013-03-03 10:52:53")
    assert row == ("mal:karthiga", "anime:mal:21", 9.0, "2013-03-03T10:52:53")


def test_item_uid_points_at_the_catalog_row():
    """The rating's uid must be the one anime_mal_csv wrote, not a variant.

    Both go through ``make_item_uid``; this pins the two together, since a
    silent drift here would make every join in Phase 3 return nothing.
    """
    from recsys.ingest.anime_mal_csv import map_mal_row

    item = map_mal_row({"anime_id": "21", "title": "One Piece"})
    rating = map_rating("someone", "21", "8", "")
    assert rating[1] == item.item_uid == "anime:mal:21"


def test_user_uid_is_namespaced_by_source():
    """MovieLens user 21 is not MAL user 21, and one table holds both."""
    assert map_rating("21", "1", "5", "")[0] == "mal:21"


@pytest.mark.parametrize("score", ["0", "", "  ", "-1", "11", "abc"])
def test_unusable_scores_are_skipped(score):
    """0 means "on the list, unrated" -- not "rated zero"."""
    assert map_rating("karthiga", "21", score, "") is None


@pytest.mark.parametrize("username,anime_id", [("", "21"), ("  ", "21"), ("karthiga", "")])
def test_rows_without_both_keys_are_skipped(username, anime_id):
    assert map_rating(username, anime_id, "9", "") is None


def test_score_keeps_its_zero_to_ten_scale():
    """MAL scores are already the unified scale; nothing is rescaled."""
    assert map_rating("u", "1", "10", "")[2] == 10.0
    assert map_rating("u", "1", "1", "")[2] == 1.0


@pytest.mark.parametrize(
    "stamp",
    [
        "",
        "   ",
        "0000-00-00 00:00:00",
        "0000-00-00",
        # What cleaning turns those into. ~97k rows of the 19M arrive this way,
        # and a real 1970 rating does not exist -- MAL launched in 2004.
        "1970-01-01 00:00:00",
        "0",
    ],
)
def test_missing_timestamps_become_none(stamp):
    assert _parse_rated_at(stamp) is None


def test_epoch_timestamps_are_accepted():
    """UserAnimeList.csv stores unix epochs where the cleaned file stores dates."""
    assert _parse_rated_at("1470009600").startswith("2016-08-01")


def test_every_fixture_row_is_either_mapped_or_skipped(rows):
    mapped = [
        map_rating(r["username"], r["anime_id"], r["my_score"], r["my_last_updated"])
        for r in rows
    ]
    assert len(mapped) == N_ROWS
    assert sum(m is not None for m in mapped) == N_KEPT


# ---- the ratings table ----------------------------------------------------


def test_upsert_ratings_is_idempotent(conn):
    rows = [("mal:a", "anime:mal:1", 9.0, None), ("mal:b", "anime:mal:1", 4.0, None)]
    assert upsert_ratings(conn, rows) == 2
    upsert_ratings(conn, rows)
    assert conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == 2


def test_upsert_ratings_overwrites_a_changed_score(conn):
    upsert_ratings(conn, [("mal:a", "anime:mal:1", 9.0, None)])
    upsert_ratings(conn, [("mal:a", "anime:mal:1", 3.0, "2020-01-01T00:00:00")])
    row = conn.execute("SELECT rating, rated_at FROM ratings").fetchone()
    assert (row["rating"], row["rated_at"]) == (3.0, "2020-01-01T00:00:00")


def test_a_rating_may_reference_an_item_that_is_not_in_the_catalog(conn):
    """No foreign key, on purpose: the two loaders must run in either order."""
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert upsert_ratings(conn, [("mal:a", "anime:mal:99999", 7.0, None)]) == 1


def test_upsert_ratings_handles_an_empty_batch(conn):
    assert upsert_ratings(conn, []) == 0


def test_migration_2_is_applied_by_init_db(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 2


# ---- the loader -----------------------------------------------------------


async def test_loader_writes_only_the_scored_rows(settings, conn):
    result = await MalRatingsLoader(settings).run(conn)

    assert result.status == "ok"
    assert (result.n_fetched, result.n_inserted, result.n_skipped) == (
        N_KEPT, N_KEPT, N_SKIPPED,
    )
    assert conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == N_KEPT


async def test_loader_is_idempotent(settings, conn):
    await MalRatingsLoader(settings).run(conn)
    await MalRatingsLoader(settings).run(conn)
    assert conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == N_KEPT


async def test_loader_handles_a_non_ascii_username(settings, conn):
    """The dump is UTF-8; Windows would open it as cp1252 and die here."""
    await MalRatingsLoader(settings).run(conn)
    row = conn.execute(
        "SELECT rating FROM ratings WHERE user_uid = ?", ("mal:ユーザー名",)
    ).fetchone()
    assert row["rating"] == 8.0


async def test_loader_records_an_ingest_run(settings, conn):
    await MalRatingsLoader(settings).run(conn)
    run = conn.execute("SELECT * FROM ingest_runs ORDER BY id DESC").fetchone()
    assert (run["source"], run["domain"], run["status"]) == ("mal_ratings", "anime", "ok")
    assert run["n_inserted"] == N_KEPT


async def test_ratings_runs_are_distinguishable_from_catalog_runs(settings, conn):
    """``ingest_runs.source`` is the seam: "mal" is the catalog, "mal_ratings" is not."""
    await MalRatingsLoader(settings).run(conn)
    sources = [r["source"] for r in conn.execute("SELECT source FROM ingest_runs")]
    assert sources == ["mal_ratings"]


async def test_target_count_limits_rows_read_not_rows_written(settings, conn):
    """What bootstrap.py --limit sets. The first 3 rows hold one unscored entry."""
    loader = MalRatingsLoader(settings)
    loader.target_count = 3
    result = await loader.run(conn)
    assert (result.n_fetched, result.n_skipped) == (2, 1)


async def test_a_ragged_line_is_skipped_not_fatal(settings, conn, tmp_path, monkeypatch):
    ragged = tmp_path / "ragged.csv"
    ragged.write_text(
        "username,anime_id,my_score,my_last_updated\n"
        "good,21,9,2013-03-03 10:52:53\n"
        "truncated,21\n"
        "alsogood,59,7,2013-03-10 13:54:51\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MAL_RATINGS_PATH", str(ragged))
    get_settings.cache_clear()

    result = await MalRatingsLoader(get_settings()).run(conn)
    assert (result.status, result.n_inserted, result.n_skipped) == ("ok", 2, 1)


async def test_a_file_without_the_required_columns_fails_the_run(conn, tmp_path, monkeypatch):
    wrong = tmp_path / "wrong.csv"
    wrong.write_text("anime_id,title\n1,Cowboy Bebop\n", encoding="utf-8")
    monkeypatch.setenv("MAL_RATINGS_PATH", str(wrong))
    get_settings.cache_clear()

    result = await MalRatingsLoader(get_settings()).run(conn)
    assert result.status == "error"
    # Named on every supported Python; 3.14's list.index no longer says which.
    assert "username" in result.error
    assert "my_score" in result.error


def test_resolve_path_rejects_a_missing_file(monkeypatch):
    monkeypatch.setenv("MAL_RATINGS_PATH", str(FIXTURE.parent / "nope.csv"))
    get_settings.cache_clear()
    with pytest.raises(FileNotFoundError, match="MAL_RATINGS_PATH"):
        MalRatingsLoader(get_settings()).resolve_path()


def test_registry_exposes_the_loader(settings):
    """The sibling contract plugs into the same seam as a catalog source."""
    from recsys.ingest.registry import available, get_ingester

    assert "anime_mal_ratings" in available()
    loader = get_ingester("anime_mal_ratings", settings)
    assert (loader.domain, loader.source) == ("anime", "mal_ratings")


def test_no_network_import_at_module_load():
    import sys

    sys.modules.pop("kagglehub", None)
    import recsys.ingest.ratings_mal_csv  # noqa: F401

    assert "kagglehub" not in sys.modules
