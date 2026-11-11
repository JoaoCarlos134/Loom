"""Covers: the three ways a work drew a blank tile, and the offline fix for them.

Each was a populated-looking gap. Music had no source of images at all; a
third of the book dump's image URLs pointed at one grey "no photo" file; and
every image URL in the 2018 anime dump named a host that no longer resolves.
Two of those three read as "covered" to any query that only asked whether the
field was null, which is why they are pinned here by what the mapper returns.
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx
import pytest

from recsys.config import get_settings
from recsys.db import connect, get_item, init_db, query_items, upsert_items
from recsys.ingest.anime_mal_csv import map_mal_row
from recsys.ingest.book_goodbooks import map_goodbooks_row
from recsys.ingest.book_openlibrary import cover_url, map_openlibrary
from recsys.ingest.music_commons import (
    CommonsMusicIngester,
    commons_urls,
    map_commons,
)
from recsys.ingest.music_deezer import map_deezer
from recsys.ingest.registry import available, get_ingester
from recsys.link import LINK_KEYS, rebuild_links
from recsys.remap import REMAPPABLE, remap
from recsys.schemas import UnifiedItem, utcnow
from recsys.works import rebuild_works

MBID = "859d0860-d480-4efd-970c-c05d5f1776b8"
FILE_PATH = "http://commons.wikimedia.org/wiki/Special:FilePath/Beyonc%C3%A9%20%28crop%29.jpg"


@pytest.fixture
def conn():
    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings.db_path)
    c = connect(settings.db_path)
    yield c
    c.close()


# ---- books ---------------------------------------------------------------


def goodbooks(image_url):
    return {"goodreads_book_id": "1", "title": "A Book", "image_url": image_url}


def test_a_goodreads_placeholder_is_not_a_cover():
    """A null lets a sibling row supply the real cover; a placeholder URL is a
    populated field that nothing is allowed to overwrite."""
    stand_in = "https://s.gr-assets.com/assets/nophoto/book/111x148-bcc042a9.png"
    assert map_goodbooks_row(goodbooks(stand_in)).image_url is None


def test_a_real_goodreads_cover_survives():
    url = "https://images.gr-assets.com/books/1447303603m/2767052.jpg"
    assert map_goodbooks_row(goodbooks(url)).image_url == url


def test_open_library_covers_are_addressed_by_cover_id():
    """Not by ISBN, which the Covers API limits to 100 lookups per five
    minutes -- a single page of the shelf is forty."""
    assert cover_url({"covers": [8231856]}) == (
        "https://covers.openlibrary.org/b/id/8231856-L.jpg"
    )


@pytest.mark.parametrize("covers", [None, [], [-1], [0], ["8231856"], [True]])
def test_a_work_without_a_usable_cover_id_has_no_cover(covers):
    """-1 is Open Library's own "known to have none"."""
    assert cover_url({"covers": covers}) is None


def test_the_first_real_cover_id_is_used():
    assert cover_url({"covers": [-1, 42, 43]}).endswith("/42-L.jpg")


def test_the_open_library_row_carries_its_cover():
    raw = {"key": "/works/OL1W", "title": "A Book", "description": "Words.",
           "_isbn": "0439023483", "covers": [42]}
    assert map_openlibrary(raw).image_url.endswith("/b/id/42-L.jpg")


def test_a_book_gets_its_cover_from_whichever_row_has_one(conn):
    """The ratings row leads with a placeholder, the text row has the cover."""
    upsert_items(conn, [
        map_goodbooks_row({**goodbooks("https://s.gr-assets.com/assets/nophoto/book/x.png"),
                           "isbn": "439023483", "ratings_count": "10"}),
        map_openlibrary({"key": "/works/OL1W", "title": "A Book", "_isbn": "0439023483",
                         "subjects": ["dystopia"], "covers": [42]}),
    ])
    rebuild_links(conn)
    assert get_item(conn, "book:goodbooks:1").image_url.endswith("/b/id/42-L.jpg")


# ---- anime ---------------------------------------------------------------


def test_the_dumps_dead_image_host_is_replaced_with_the_live_one():
    raw = {"anime_id": "1", "title": "Cowboy Bebop",
           "image_url": "https://myanimelist.cdn-dena.com/images/anime/4/19644.jpg"}
    assert map_mal_row(raw).image_url == "https://cdn.myanimelist.net/images/anime/4/19644.jpg"


def test_an_image_already_on_the_live_host_is_left_alone():
    url = "https://cdn.myanimelist.net/images/anime/4/19644.jpg"
    assert map_mal_row({"anime_id": "1", "title": "X", "image_url": url}).image_url == url


# ---- music ---------------------------------------------------------------


def binding(mbid=MBID, image=FILE_PATH, entity="http://www.wikidata.org/entity/Q36153"):
    return {"mbid": {"value": mbid}, "image": {"value": image}, "item": {"value": entity}}


def artist(mbid=MBID, name="Beyoncé", **kw):
    return UnifiedItem(
        domain="music", source="musicbrainz", source_id=mbid, title=name,
        external_ids={"musicbrainz_id": mbid}, fetched_at=utcnow(), **kw,
    )


def portrait(mbid=MBID, name="Beyoncé"):
    return map_commons({"_mbid": mbid, "_name": name, "image": FILE_PATH,
                        "item": "http://www.wikidata.org/entity/Q36153"})


def test_a_commons_image_becomes_a_thumbnail_and_a_credit():
    """The bare URL redirects to the original upload, routinely megabytes; the
    file page is where the author and licence are stated."""
    image, credit = commons_urls(FILE_PATH, 500)
    assert image == ("https://commons.wikimedia.org/wiki/Special:FilePath/"
                     "Beyonc%C3%A9%20%28crop%29.jpg?width=500")
    assert credit == "https://commons.wikimedia.org/wiki/File:Beyonc%C3%A9%20%28crop%29.jpg"


@pytest.mark.parametrize("value", [
    "", "https://example.org/wiki/Special:FilePath/x.jpg",
    "http://commons.wikimedia.org/wiki/Special:FilePath/",
    "http://commons.wikimedia.org/wiki/File:x.jpg",
])
def test_anything_that_is_not_a_commons_file_is_refused(value):
    assert commons_urls(value, 500) is None


def test_a_portrait_row_is_keyed_by_the_artists_mbid():
    row = portrait()
    assert row.item_uid == f"music:commons:{MBID}"
    assert row.external_ids["musicbrainz_id"] == MBID
    assert row.external_ids["wikidata_id"] == "Q36153"
    assert row.external_ids["image_credit_url"].startswith(
        "https://commons.wikimedia.org/wiki/File:")


def test_a_portrait_row_claims_nothing_but_the_portrait():
    row = portrait()
    assert row.tags == [] and row.description is None and row.popularity is None


@pytest.mark.parametrize("raw", [
    {"_mbid": "", "_name": "X", "image": FILE_PATH},
    {"_mbid": MBID, "_name": "", "image": FILE_PATH},
    {"_mbid": MBID, "_name": "X", "image": "not a url"},
])
def test_an_unusable_binding_is_skipped(raw):
    assert map_commons(raw) is None


def test_an_mbid_is_a_link_key():
    assert "musicbrainz_id" in LINK_KEYS


def test_the_artist_wears_the_portrait(conn):
    upsert_items(conn, [artist(tags=["pop"], popularity=100.0), portrait()])
    rebuild_links(conn)
    rebuild_works(conn)
    total, listed = query_items(conn, domain="music")
    assert total == 1                                   # one artist, not two
    assert listed[0].item_uid == f"music:musicbrainz:{MBID}"
    assert listed[0].image_url.endswith("?width=500")


def test_the_credit_travels_with_the_portrait(conn):
    upsert_items(conn, [artist(tags=["pop"]), portrait()])
    rebuild_links(conn)
    ids = get_item(conn, f"music:musicbrainz:{MBID}").external_ids
    assert ids["image_credit_url"].startswith("https://commons.wikimedia.org/wiki/File:")


def test_a_portrait_never_represents_the_artist_even_when_the_artist_row_is_bare(conn):
    """Load-bearing: the embedding matrix holds representatives only, so a
    portrait winning the election drops the artist out of every recommendation
    until the matrix is rebuilt. With nothing else to choose on, the election
    falls to the uid -- on which ``commons`` sorts below ``musicbrainz``."""
    upsert_items(conn, [artist(), portrait()])   # no tags, no audience, no text
    rebuild_links(conn)
    primary = conn.execute(
        "SELECT item_uid FROM links WHERE is_primary = 1"
    ).fetchone()[0]
    assert primary == f"music:musicbrainz:{MBID}"


def test_the_ingester_asks_about_the_catalogs_artists_and_no_one_else(conn, monkeypatch):
    other_id = "afb680f2-b6eb-4cd7-a70b-a63b25c763d5"
    upsert_items(conn, [
        artist(popularity=5.0),
        artist(other_id, "Bruno Mars", popularity=9.0),
        artist("not-a-uuid'; DROP", "Bobby Tables"),
        portrait(),                                   # its own rows are not input
    ])
    ingester = CommonsMusicIngester(get_settings())
    asked: list[list[str]] = []

    async def fake_images(mbids):
        asked.append(list(mbids))
        return [
            binding(),
            binding(image=FILE_PATH.replace("Beyonc", "Aaa")),   # a second image
            binding(mbid="00000000-0000-0000-0000-000000000000"),  # nobody we asked about
        ]

    monkeypatch.setattr(ingester, "_images", fake_images)

    async def collect():
        return [raw async for raw in ingester.fetch()]

    records = asyncio.run(collect())
    assert asked == [[other_id, MBID]]                  # best-known first, UUIDs only
    assert [r["_mbid"] for r in records] == [MBID]
    assert "Aaa" in records[0]["image"]                 # the smallest, so a re-run agrees
    assert ingester.map_item(records[0]).title == "Beyoncé"


def test_wikidata_is_asked_once_and_then_answered_from_disk(conn):
    """Driven through a mock transport: the request it would have sent, and
    that a second ask for the same artists costs Wikidata nothing."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": {"bindings": [binding()]}})

    ingester = CommonsMusicIngester(get_settings())
    ingester._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def ask_twice():
        first = await ingester._images([MBID])
        second = await ingester._images([MBID])
        await ingester.aclose()
        return first, second

    first, second = asyncio.run(ask_twice())
    assert first == second == [binding()]
    assert len(seen) == 1 and ingester.n_cached == 1
    assert seen[0].method == "POST"
    assert MBID in parse_qs(seen[0].content.decode())["query"][0]


def test_a_refusal_from_wikidata_is_an_error_not_an_empty_answer(conn):
    """An empty list would be cached as "these artists have no portrait"."""
    ingester = CommonsMusicIngester(get_settings())
    ingester._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(400))
    )

    async def ask():
        try:
            return await ingester._images([MBID])
        finally:
            await ingester.aclose()

    with pytest.raises(RuntimeError, match="HTTP 400"):
        asyncio.run(ask())
    assert ingester.cache.get(f"{ingester.url}?mbids={MBID}") is None


def deezer_row(mbid=MBID, name="Beyoncé", deezer_id="145"):
    return map_deezer({"_mbid": mbid, "_name": name, "_deezer_id": deezer_id})


def test_a_deezer_picture_is_addressed_by_the_id_already_in_the_catalog():
    row = deezer_row()
    assert row.item_uid == f"music:deezer:{MBID}"
    assert row.image_url == "https://api.deezer.com/artist/145/image?size=big"
    assert row.tags == [] and row.popularity is None


@pytest.mark.parametrize("deezer_id", ["", None, "145/../x", "abc"])
def test_a_deezer_id_that_is_not_a_number_never_reaches_a_url(deezer_id):
    assert deezer_row(deezer_id=deezer_id) is None


def test_an_artist_commons_lacks_gets_the_deezer_picture(conn):
    upsert_items(conn, [artist(tags=["pop"]), deezer_row()])
    rebuild_links(conn)
    assert "api.deezer.com" in get_item(conn, f"music:musicbrainz:{MBID}").image_url


def test_a_free_portrait_is_never_displaced_by_the_deezer_one(conn):
    """Commons where it has a picture, Deezer only where it does not. Whichever
    was ingested first: the choice is by uid order, not arrival."""
    upsert_items(conn, [artist(tags=["pop"]), deezer_row(), portrait()])
    rebuild_links(conn)
    rebuild_works(conn)
    shown = get_item(conn, f"music:musicbrainz:{MBID}")
    assert "commons.wikimedia.org" in shown.image_url
    assert query_items(conn, domain="music")[1][0].image_url == shown.image_url


def test_a_deezer_row_never_represents_the_artist_either(conn):
    upsert_items(conn, [artist(), deezer_row()])
    rebuild_links(conn)
    assert conn.execute(
        "SELECT item_uid FROM links WHERE is_primary = 1"
    ).fetchone()[0] == f"music:musicbrainz:{MBID}"


def test_the_deezer_ingester_makes_rows_from_catalog_ids_alone(conn):
    """No request: the address is a function of an id already stored."""
    with_id = artist(popularity=5.0).model_copy(
        update={"external_ids": {"musicbrainz_id": MBID, "deezer_id": "145"}})
    upsert_items(conn, [with_id, artist("afb680f2-b6eb-4cd7-a70b-a63b25c763d5", "No Deezer")])
    ingester = get_ingester("music_deezer", get_settings())

    async def collect():
        return [raw async for raw in ingester.fetch()]

    records = asyncio.run(collect())
    assert [r["_mbid"] for r in records] == [MBID]
    assert ingester.map_item(records[0]).image_url.endswith("/artist/145/image?size=big")


def test_the_registry_exposes_the_portrait_ingester():
    assert "music_commons" in available()
    assert get_ingester("music_commons", get_settings()).source == "commons"


# ---- remap ---------------------------------------------------------------


class Mapper:
    """Stands in for an ingester: a domain, a source, and today's mapper."""

    domain = "book"
    source = "goodbooks"

    def map_item(self, raw):
        if raw.get("explode"):
            raise RuntimeError("cannot map this")
        return map_goodbooks_row(raw)


def stored(conn, raw, **over):
    """A row as an older mapper left it: payload intact, fields as they were."""
    item = map_goodbooks_row(raw).model_copy(update=over)
    upsert_items(conn, [item])
    return item.item_uid


def test_a_remap_applies_todays_mapper_to_a_stored_payload(conn):
    stand_in = "https://s.gr-assets.com/assets/nophoto/book/x.png"
    uid = stored(conn, goodbooks(stand_in), image_url=stand_in)

    result = remap(conn, Mapper(), ["image_url"])
    assert (result.n_rows, result.n_changed, result.n_unmapped) == (1, 1, 0)
    assert get_item(conn, uid).image_url is None


def test_a_remap_writes_only_the_fields_it_was_given(conn):
    """The stored payload is not always the mapper's whole input: goodbooks
    takes its tags from a second file, so remapping yields none. Writing that
    back would erase every tag list to fix a cover URL."""
    stand_in = "https://s.gr-assets.com/assets/nophoto/book/x.png"
    uid = stored(conn, goodbooks(stand_in), image_url=stand_in, tags=["dystopia", "ya"])

    remap(conn, Mapper(), ["image_url"])
    assert get_item(conn, uid).tags == ["dystopia", "ya"]


def test_a_dry_run_shows_when_a_remap_would_empty_a_field(conn):
    """Asking for the field the payload cannot rebuild is not refused -- an
    emptied field is sometimes the fix -- but it is counted where a dry run
    will show it, before ten thousand tag lists go."""
    stored(conn, goodbooks("https://x/a.jpg"), tags=["dystopia", "ya"])

    result = remap(conn, Mapper(), ["tags"], dry_run=True)
    assert (result.n_changed, result.n_emptied) == (1, 1)


def test_a_dry_run_reports_and_changes_nothing(conn):
    stand_in = "https://s.gr-assets.com/assets/nophoto/book/x.png"
    uid = stored(conn, goodbooks(stand_in), image_url=stand_in)

    result = remap(conn, Mapper(), ["image_url"], dry_run=True)
    assert result.n_changed == 1 and result.dry_run
    assert get_item(conn, uid).image_url == stand_in


def test_a_second_remap_finds_nothing_to_do(conn):
    stand_in = "https://s.gr-assets.com/assets/nophoto/book/x.png"
    stored(conn, goodbooks(stand_in), image_url=stand_in)
    remap(conn, Mapper(), ["image_url"])
    assert remap(conn, Mapper(), ["image_url"]).n_changed == 0


def test_a_remap_leaves_other_sources_alone(conn):
    upsert_items(conn, [UnifiedItem(
        domain="book", source="openlibrary", source_id="1", title="Other",
        image_url="https://example.org/a.jpg", raw_json={"x": 1}, fetched_at=utcnow(),
    )])
    assert remap(conn, Mapper(), ["image_url"]).n_rows == 0


def test_a_payload_that_no_longer_maps_is_counted_not_deleted(conn):
    """Deleting catalog rows is prune.py's job and nobody else's."""
    uid = stored(conn, {**goodbooks("https://x/a.jpg"), "explode": True})
    result = remap(conn, Mapper(), ["image_url"])
    assert result.n_unmapped == 1 and result.n_changed == 0
    assert get_item(conn, uid) is not None


@pytest.mark.parametrize("fields", [[], ["item_uid"], ["raw_json"], ["fetched_at"]])
def test_identity_and_bookkeeping_cannot_be_remapped(conn, fields):
    with pytest.raises(ValueError, match="cannot remap"):
        remap(conn, Mapper(), fields)


def test_every_remappable_field_is_a_real_column(conn):
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    assert set(REMAPPABLE) <= columns
