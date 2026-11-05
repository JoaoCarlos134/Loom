"""goodbooks-10k ratings.csv row -> ``ratings`` row, and the loader around it.

Driven by `tests/fixtures/goodbooks/ratings.csv` beside the catalog fixture:
four rows that map, and one each for the cases that decide what is skipped --
a zero, a book not in books.csv, a missing user, a non-number, and an
out-of-range score.

Nine rows in; four survive.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from recsys.config import Settings, get_settings
from recsys.db import connect, init_db
from recsys.ingest.book_goodbooks import map_goodbooks_row
from recsys.ingest.ratings_goodbooks import GoodbooksRatingsLoader, map_rating

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "goodbooks"

N_ROWS = 9
N_KEPT = 4

IDS = {"1": "2767052", "2": "3"}


@pytest.fixture
def settings(monkeypatch) -> Settings:
    monkeypatch.setenv("GOODBOOKS_PATH", str(FIXTURE_DIR))
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "ratings.db"
    init_db(path)
    c = connect(path)
    yield c
    c.close()


# ---- mapping --------------------------------------------------------------


def test_maps_a_row_onto_the_ten_point_scale():
    assert map_rating("7", "1", "4", IDS) == ("goodbooks:7", "book:goodbooks:2767052", 8.0, None)


def test_item_uid_points_at_the_catalog_row():
    """The dump's book_id 1 is goodreads 2767052, which is what the catalog keys on."""
    item = map_goodbooks_row({"goodreads_book_id": "2767052", "title": "The Hunger Games"})
    assert map_rating("7", "1", "5", IDS)[1] == item.item_uid


@pytest.mark.parametrize(
    ("user", "book", "rating"),
    [("7", "1", "0"), ("7", "1", "6"), ("7", "1", "x"), ("", "1", "5"), ("7", "404", "5")],
)
def test_skips_unusable_rows(user, book, rating):
    assert map_rating(user, book, rating, IDS) is None


# ---- loader ---------------------------------------------------------------


def test_translates_book_ids_from_books_csv(settings):
    ids = GoodbooksRatingsLoader(settings).goodreads_ids()
    assert ids["1"] == "2767052"
    assert ids["79"] == "1381"


def test_run_writes_the_kept_rows(settings, conn):
    loader = GoodbooksRatingsLoader(settings)
    result = asyncio.run(loader.run(conn))

    assert result.status == "ok"
    assert result.n_fetched == N_KEPT
    assert result.n_skipped == N_ROWS - N_KEPT
    rows = conn.execute(
        "SELECT user_uid, item_uid, rating FROM ratings ORDER BY user_uid, item_uid"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("goodbooks:1", "book:goodbooks:2767052", 10.0),
        ("goodbooks:1", "book:goodbooks:3", 8.0),
        ("goodbooks:2", "book:goodbooks:2767052", 6.0),
        ("goodbooks:4", "book:goodbooks:5470", 4.0),
    ]


def test_target_count_bounds_rows_read(settings, conn, monkeypatch):
    monkeypatch.setenv("GOODBOOKS_RATINGS_TARGET_COUNT", "2")
    get_settings.cache_clear()
    result = asyncio.run(GoodbooksRatingsLoader(get_settings()).run(conn))
    assert result.n_fetched == 2


def test_is_registered():
    from recsys.ingest.registry import available

    assert "book_goodbooks_ratings" in available()


def test_a_file_without_the_required_columns_names_them(conn, tmp_path, monkeypatch):
    """The error says which column is missing on every supported Python.

    3.14 stopped naming the value in ``list.index``'s ValueError, which is what
    the message used to be built from.
    """
    (tmp_path / "books.csv").write_text(
        (FIXTURE_DIR / "books.csv").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "ratings.csv").write_text("user_id,stars\n1,5\n", encoding="utf-8")
    monkeypatch.setenv("GOODBOOKS_PATH", str(tmp_path))
    get_settings.cache_clear()

    result = asyncio.run(GoodbooksRatingsLoader(get_settings()).run(conn))
    assert result.status == "error"
    assert "book_id" in result.error and "rating" in result.error
