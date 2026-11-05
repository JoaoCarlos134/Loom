"""Open Library work records -> UnifiedItem, and the linking they depend on.

This source exists for one reason: goodbooks-10k ships no synopsis, leaving the
book domain at 0% description coverage while films sat at 99%. Books share no
identifier with any other domain, so text and subjects are the only route from a
novel to an anime -- which makes this the largest single hole in the bridge.
"""

from __future__ import annotations

import pytest

from recsys.config import Settings, get_settings
from recsys.db import connect, init_db, query_items, upsert_items
from recsys.ingest.book_openlibrary import (
    OpenLibraryIngester,
    clean_description,
    is_descriptive,
    map_openlibrary,
)
from recsys.link import LINK_KEYS, rebuild_links
from recsys.popularity import rebuild_popularity
from recsys.schemas import UnifiedItem


def work(**over) -> dict:
    base = {
        "key": "/works/OL5735363W",
        "title": "The Hunger Games",
        "description": "Katniss volunteers for a televised fight to the death.",
        "subjects": ["dystopia", "survival", "Accessible book", "science fiction"],
        "_isbn": "0439023483",
        "_authors": [{"name": "Suzanne Collins"}],
    }
    base.update(over)
    return base


def goodbooks_row(isbn="0439023483", **over) -> UnifiedItem:
    kwargs = dict(
        domain="book", source="goodbooks", source_id="2767052", title="The Hunger Games",
        external_ids={"isbn": isbn}, n_ratings=4_780_653, popularity=4_780_653.0,
        mean_rating=8.68, tags=["young-adult", "dystopian"],
    )
    kwargs.update(over)
    return UnifiedItem(**kwargs)


@pytest.fixture
def settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "ol.db"
    init_db(path)
    c = connect(path)
    yield c
    c.close()


@pytest.fixture
def seeded(settings):
    init_db()
    c = connect()
    yield c
    c.close()


# ---- mapping --------------------------------------------------------------


def test_a_work_maps_into_the_book_domain():
    item = map_openlibrary(work())
    assert item.domain == "book"
    assert item.item_uid == "book:openlibrary:0439023483"


def test_it_is_keyed_by_isbn_not_by_the_open_library_work():
    """Two Goodreads books can resolve to one Open Library work -- different
    editions of one novel. Keying on the work id would collapse them into a row
    that links back to only one."""
    a = map_openlibrary(work(_isbn="1111111111"))
    b = map_openlibrary(work(_isbn="2222222222"))
    assert a.item_uid != b.item_uid
    assert a.external_ids["openlibrary_work"] == b.external_ids["openlibrary_work"]


def test_the_description_is_the_point():
    assert map_openlibrary(work()).description.startswith("Katniss volunteers")


def test_a_description_stored_as_an_object_is_unwrapped():
    """Open Library stores it either as a string or as {"value": ...}."""
    assert clean_description({"value": "A story."}) == "A story."
    assert clean_description("A story.") == "A story."


@pytest.mark.parametrize("value", [None, 42, "", "   ", {"value": ""}, {}])
def test_unusable_descriptions_become_none(value):
    assert clean_description(value) is None


def test_a_trailing_source_credit_is_trimmed():
    text = "A real synopsis long enough to survive the cut. " * 4
    assert "Source:" not in (clean_description(text + "\n\n[Source: Wikipedia]") or "")


def test_subjects_become_tags():
    tags = set(map_openlibrary(work()).tags)
    assert {"dystopia", "survival"} <= tags


@pytest.mark.parametrize(
    "subject",
    ["Accessible book", "Protected DAISY", "In library", "OverDrive",
     "American fiction (fictional works by one author)", "Translations into French"],
)
def test_cataloguing_subjects_are_dropped(subject):
    """These classify a record, not a story."""
    assert not is_descriptive(subject)


@pytest.mark.parametrize(
    "subject",
    ["series:harry_potter", "nyt:series_books=2008-03-15", "new york times bestseller",
     "award winner"],
)
def test_machine_and_sales_headings_are_dropped(subject):
    """Open Library carries a lot of catalogue plumbing among its subjects, and
    a sales fact is not a description of a story."""
    assert not is_descriptive(subject)


@pytest.mark.parametrize(
    "subject", ["dystopia", "coming of age", "magical realism", "Fiction: general"]
)
def test_real_subjects_survive(subject):
    """A heading that merely contains a colon has a space after it and stays."""
    assert is_descriptive(subject)


def test_subjects_are_capped():
    item = map_openlibrary(work(subjects=[f"subject {i}" for i in range(80)]), max_subjects=5)
    assert len(item.tags) == 5


def test_a_row_with_neither_prose_nor_subjects_is_skipped():
    """It would add nothing the goodbooks row does not have, and would only
    dilute the primary election."""
    assert map_openlibrary(work(description=None, subjects=[])) is None


def test_a_row_with_subjects_but_no_prose_is_still_worth_keeping():
    assert map_openlibrary(work(description=None)) is not None


@pytest.mark.parametrize("bad", [{}, {"_isbn": "", "title": "x"}, {"_isbn": "1", "title": ""}])
def test_unusable_records_are_skipped(bad):
    assert map_openlibrary(bad) is None


def test_no_rating_or_audience_is_invented():
    """Open Library has neither. The linked goodbooks row has both."""
    item = map_openlibrary(work())
    assert (item.popularity, item.mean_rating, item.n_ratings) == (None, None, None)


# ---- linking to the goodbooks row -----------------------------------------


def test_isbn_is_a_link_key():
    assert "isbn" in LINK_KEYS


def test_the_two_book_sources_collapse_to_one_work(conn):
    upsert_items(conn, [goodbooks_row(), map_openlibrary(work())])
    result = rebuild_links(conn)
    assert (result.n_works, result.n_linked) == (1, 2)
    assert query_items(conn, domain="book")[0] == 1


def test_the_row_with_the_synopsis_represents_the_work(conn):
    """Which is the whole point of fetching it."""
    upsert_items(conn, [goodbooks_row(), map_openlibrary(work())])
    rebuild_links(conn)
    _, items = query_items(conn, domain="book")
    assert items[0].source == "openlibrary"
    assert items[0].description


def test_different_isbns_are_not_linked(conn):
    """An ISBN identifies a printing. Under-linking is the safe direction."""
    upsert_items(conn, [goodbooks_row(isbn="1111111111"), map_openlibrary(work())])
    assert rebuild_links(conn).n_works == 0
    assert query_items(conn, domain="book")[0] == 2


def test_the_work_keeps_its_audience_even_though_the_winner_has_none(conn):
    """Open Library carries no audience count, so the row with the synopsis
    would sort below everything -- ordering happens in SQL against the stored
    value, so filling the gap after the query would come too late."""
    upsert_items(conn, [goodbooks_row(), map_openlibrary(work())])
    rebuild_links(conn)
    rebuild_popularity(conn)

    _, items = query_items(conn, domain="book")
    primary = items[0]
    stored = conn.execute(
        "SELECT value FROM popularity_norm WHERE item_uid = ?", (primary.item_uid,)
    ).fetchone()
    assert primary.source == "openlibrary"
    assert stored is not None and stored["value"] == 4_780_653.0


def test_a_book_with_prose_outranks_one_without(conn):
    """The regression this guards: before popularity was shared within a work,
    an enriched book sorted below every un-enriched one."""
    upsert_items(conn, [
        goodbooks_row(), map_openlibrary(work()),
        goodbooks_row(isbn="9999999999", source_id="999", title="Unenriched",
                      n_ratings=1000, popularity=1000.0),
    ])
    rebuild_links(conn)
    rebuild_popularity(conn)
    _, items = query_items(conn, domain="book")
    assert items[0].title.startswith("The Hunger Games")


# ---- what it decides to fetch ---------------------------------------------


def test_isbns_come_from_the_catalog(seeded, settings):
    upsert_items(seeded, [goodbooks_row(isbn="1111111111"), goodbooks_row(
        isbn="2222222222", source_id="3")])
    assert set(OpenLibraryIngester(settings).isbns_to_fetch()) == {
        "1111111111", "2222222222"}


def test_its_own_rows_are_not_used_as_input(seeded, settings):
    upsert_items(seeded, [goodbooks_row(), map_openlibrary(work())])
    assert OpenLibraryIngester(settings).isbns_to_fetch() == ["0439023483"]


def test_the_most_read_books_come_first(seeded, settings):
    upsert_items(seeded, [
        goodbooks_row(isbn="1111111111", source_id="1", popularity=10.0),
        goodbooks_row(isbn="2222222222", source_id="2", popularity=9999.0),
    ])
    assert OpenLibraryIngester(settings).isbns_to_fetch()[0] == "2222222222"


async def test_no_books_means_nothing_to_fetch(seeded, settings):
    """book_goodbooks has to run first; saying so beats crashing."""
    result = await OpenLibraryIngester(settings).run(seeded)
    assert (result.status, result.n_fetched) == ("ok", 0)


def test_registry_exposes_the_ingester(settings):
    from recsys.ingest.registry import available, get_ingester

    assert "book_openlibrary" in available()
    ingester = get_ingester("book_openlibrary", settings)
    assert (ingester.domain, ingester.source) == ("book", "openlibrary")


# ---- covers ---------------------------------------------------------------
#
# Open Library's cover service answers by ISBN at 100 requests per IP every
# five minutes, and by cover id without a limit. The ISBN lookup is a server
# side resolution of the edition's cover id -- which the batch call this
# ingester already makes returns in `details.covers`. So the cover is taken by
# id from data already fetched, and no cover request is made at ingest at all.


def test_the_editions_cover_is_the_books_image():
    item = map_openlibrary(work(_edition_covers=[8231856], covers=[111]))
    assert item.image_url == "https://covers.openlibrary.org/b/id/8231856-M.jpg"


def test_the_works_cover_is_the_fallback():
    """-1 is how Open Library marks a deleted cover."""
    item = map_openlibrary(work(_edition_covers=[], covers=[-1, 678]))
    assert item.image_url == "https://covers.openlibrary.org/b/id/678-M.jpg"


@pytest.mark.parametrize("covers", [None, [], [-1], ["x"], [0]])
def test_no_usable_cover_is_no_image(covers):
    assert map_openlibrary(work(covers=covers)).image_url is None


def test_a_record_with_only_a_cover_is_kept_to_lend_it():
    """No prose and no subjects still adds something the goodbooks row may
    lack. It loses the primary election and lends the cover through
    fill_from_siblings."""
    item = map_openlibrary(work(description=None, subjects=[], _edition_covers=[5]))
    assert item is not None and item.image_url.endswith("/5-M.jpg")


def test_a_cover_reaches_a_book_whose_goodreads_cover_was_a_placeholder(conn):
    from recsys.db import get_item, upsert_items

    upsert_items(conn, [
        goodbooks_row(image_url=None),
        map_openlibrary(work(description=None, subjects=[], _edition_covers=[5])),
    ])
    rebuild_links(conn)
    item = get_item(conn, "book:goodbooks:2767052")
    assert item.image_url == "https://covers.openlibrary.org/b/id/5-M.jpg"


async def test_the_batch_call_carries_the_editions_cover_ids(seeded, settings, monkeypatch):
    from recsys.db import upsert_items

    async def fake(self, path, params=None):
        if path == "/api/books":
            return {"ISBN:0439023483": {"details": {
                "title": "The Hunger Games", "covers": [8231856],
                "works": [{"key": "/works/OL5735363W"}],
            }}}
        return {"key": "/works/OL5735363W", "title": "The Hunger Games",
                "description": "Katniss.", "subjects": ["dystopia"]}

    monkeypatch.setattr(OpenLibraryIngester, "_get_json", fake)
    upsert_items(seeded, [goodbooks_row()])
    ingester = OpenLibraryIngester(settings)
    records = [r async for r in ingester.fetch()]
    assert records[0]["_edition_covers"] == [8231856]
    assert ingester.map_item(records[0]).image_url.endswith("/8231856-M.jpg")
