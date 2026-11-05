"""goodbooks-10k rows -> UnifiedItem, and the shelf filtering around them.

Driven by `tests/fixtures/goodbooks/`: five real books cut from the dump with
their shelves, chosen to cover a 0-5 rating that must be rescaled, an ISBN whose
leading zero the CSV dropped, and *The Odyssey* -- published in 720 BC, which
the unified schema will not accept as a year.
"""

from __future__ import annotations

import copy
import csv
from pathlib import Path

import pytest

from recsys.config import Settings, get_settings
from recsys.db import connect, init_db
from recsys.ingest.book_goodbooks import (
    RATING_SCALE,
    GoodbooksIngester,
    is_descriptive,
    map_goodbooks_row,
    normalise_isbn,
)
from recsys.schemas import UnifiedItem

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "goodbooks"


@pytest.fixture
def rows() -> list[dict]:
    with (FIXTURE_DIR / "books.csv").open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture
def hunger_games(rows) -> dict:
    return copy.deepcopy(next(r for r in rows if r["goodreads_book_id"] == "2767052"))


@pytest.fixture
def odyssey(rows) -> dict:
    return copy.deepcopy(next(r for r in rows if r["goodreads_book_id"] == "1381"))


@pytest.fixture
def settings(monkeypatch) -> Settings:
    monkeypatch.setenv("GOODBOOKS_PATH", str(FIXTURE_DIR))
    get_settings.cache_clear()
    return get_settings()


# ---- mapping --------------------------------------------------------------


def test_every_fixture_row_maps(rows):
    items = [map_goodbooks_row(r) for r in rows]
    assert all(isinstance(i, UnifiedItem) for i in items)
    assert len(items) == 5


def test_the_uid_uses_the_goodreads_id_not_the_dumps_own(hunger_games):
    """book_id is 1..10000 and local to this dump; goodreads_book_id is the id
    the wider world uses and what another Goodreads source would join on."""
    item = map_goodbooks_row(hunger_games)
    assert item.item_uid == "book:goodbooks:2767052"
    assert item.external_ids["goodreads_book_id"] == "2767052"


def test_ratings_are_rescaled_from_five_to_ten(hunger_games):
    """Goodreads scores out of 5; every other source in the catalog is 0-10."""
    raw = float(hunger_games["average_rating"])
    assert map_goodbooks_row(hunger_games).mean_rating == pytest.approx(raw * RATING_SCALE)
    assert 0 < map_goodbooks_row(hunger_games).mean_rating <= 10


def test_authors_become_creators(hunger_games):
    creators = map_goodbooks_row(hunger_games).creators
    assert ("Suzanne Collins", "author") in [(c.name, c.role) for c in creators]


def test_multiple_authors_are_split(hunger_games):
    hunger_games["authors"] = "Terry Pratchett, Neil Gaiman"
    names = [c.name for c in map_goodbooks_row(hunger_games).creators]
    assert names == ["Terry Pratchett", "Neil Gaiman"]


def test_ratings_count_is_the_popularity_signal(hunger_games):
    item = map_goodbooks_row(hunger_games)
    assert item.popularity == float(item.n_ratings)
    assert item.n_ratings > 1000


def test_this_dump_has_no_synopsis(hunger_games):
    """A documented gap, not an oversight -- book_openlibrary closes it."""
    assert map_goodbooks_row(hunger_games).description is None


# ---- the awkward columns --------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("439023483", "0439023483"), ("0439023483", "0439023483"),
     ("043-902 3483", "0439023483"), ("", None), (None, None)],
)
def test_isbns_are_zero_padded(raw, expected):
    """The CSV stores ISBNs numerically, so every one that begins with a zero
    arrives short and fails to resolve against Open Library."""
    assert normalise_isbn(raw) == expected


def test_an_ancient_publication_year_is_dropped(odyssey):
    """The Odyssey is dated -720 here. The schema accepts 1000-2100, and losing
    the year beats inventing one."""
    assert float(odyssey["original_publication_year"]) < 0
    assert map_goodbooks_row(odyssey).year is None
    assert map_goodbooks_row(odyssey).title  # the book itself still maps


def test_an_ordinary_year_survives(hunger_games):
    assert map_goodbooks_row(hunger_games).year == 2008


def test_a_row_without_any_title_is_skipped(hunger_games):
    hunger_games["title"] = hunger_games["original_title"] = ""
    assert map_goodbooks_row(hunger_games) is None


def test_a_row_without_an_id_is_skipped(hunger_games):
    hunger_games["goodreads_book_id"] = ""
    assert map_goodbooks_row(hunger_games) is None


# ---- shelves are not tags until they are filtered -------------------------


@pytest.mark.parametrize(
    "shelf",
    ["to-read", "currently-reading", "favorites", "books-i-own", "owned", "my-books",
     "kindle", "audiobook", "series", "default", "tbr", "re-read", "wish-list"],
)
def test_library_management_shelves_are_excluded(shelf):
    """`owned` matching `owned` would be a similarity signal that means nothing."""
    assert not is_descriptive(shelf)


@pytest.mark.parametrize(
    "shelf",
    ["dystopian", "young-adult", "science-fiction", "historical-fiction",
     "magical-realism", "romance"],
)
def test_descriptive_shelves_survive(shelf):
    assert is_descriptive(shelf)


@pytest.mark.parametrize("shelf", ["-", "--1-", "--122-", "", "  ", "12", "a"])
def test_junk_shelf_names_are_excluded(shelf):
    """This dump's tag list is full of them."""
    assert not is_descriptive(shelf)


def test_shelves_reach_the_tags(settings):
    ingester = GoodbooksIngester(settings)
    shelves = ingester.shelves_by_book()
    assert "2767052" in shelves
    tags = set(shelves["2767052"])
    assert {"dystopian", "young-adult"} <= tags
    assert not tags & {"to-read", "currently-reading", "favorites"}


def test_shelves_are_ordered_by_how_many_readers_used_them(settings):
    shelves = GoodbooksIngester(settings).shelves_by_book()["2767052"]
    assert shelves[0] == "young-adult"  # the most-shelved descriptive one


def test_the_tag_cap_is_respected(settings):
    ingester = GoodbooksIngester(settings)
    ingester.max_tags = 3
    assert len(ingester.shelves_by_book()["2767052"]) == 3


# ---- the ingester ---------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "books.db"
    init_db(path)
    c = connect(path)
    yield c
    c.close()


async def test_run_populates_the_book_domain(settings, conn):
    result = await GoodbooksIngester(settings).run(conn)
    assert result.status == "ok"
    assert (result.n_fetched, result.n_inserted) == (5, 5)
    domains = {r[0] for r in conn.execute("SELECT DISTINCT domain FROM items")}
    assert domains == {"book"}


async def test_run_is_idempotent(settings, conn):
    await GoodbooksIngester(settings).run(conn)
    result = await GoodbooksIngester(settings).run(conn)
    assert (result.n_inserted, result.n_updated) == (0, 5)


async def test_books_carry_their_shelves_after_ingest(settings, conn):
    import json

    await GoodbooksIngester(settings).run(conn)
    row = conn.execute(
        "SELECT tags FROM items WHERE item_uid='book:goodbooks:2767052'"
    ).fetchone()
    assert "dystopian" in json.loads(row["tags"])


async def test_target_count_limits_the_read(settings, conn):
    ingester = GoodbooksIngester(settings)
    ingester.target_count = 2  # what bootstrap.py --limit sets
    result = await ingester.run(conn)
    assert result.n_fetched == 2


def test_a_missing_local_file_is_reported_clearly(monkeypatch, tmp_path):
    monkeypatch.setenv("GOODBOOKS_PATH", str(tmp_path))
    get_settings.cache_clear()
    with pytest.raises(FileNotFoundError, match="GOODBOOKS_PATH"):
        GoodbooksIngester(get_settings()).read_csv("books.csv")


def test_registry_exposes_the_ingester(settings):
    from recsys.ingest.registry import available, get_ingester

    assert "book_goodbooks" in available()
    ingester = get_ingester("book_goodbooks", settings)
    assert (ingester.domain, ingester.source) == ("book", "goodbooks")


def test_no_network_at_module_load():
    """A local GOODBOOKS_PATH must make the whole ingester offline."""
    import recsys.ingest.book_goodbooks as mod

    assert mod.FILES == ("books.csv", "book_tags.csv", "tags.csv")


# ---- covers ---------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://s.gr-assets.com/assets/nophoto/book/111x148-bcc042a9c91a29c1d680899eff700a03.png",
    "https://s.gr-assets.com/assets/nophoto/book/50x75-a91bf249278a81aabab721ef782c4a74.png",
])
def test_a_goodreads_placeholder_is_no_cover(hunger_games, url):
    """3,332 of goodbooks-10k's 10,000 books carry Goodreads' "nophoto"
    placeholder. Stored as a cover it hid the gap from fill_from_siblings,
    which only fills a missing value, and showed a grey book on the card."""
    hunger_games["image_url"] = url
    assert map_goodbooks_row(hunger_games).image_url is None


def test_a_real_cover_survives(hunger_games):
    assert map_goodbooks_row(hunger_games).image_url.endswith("/2767052.jpg")
