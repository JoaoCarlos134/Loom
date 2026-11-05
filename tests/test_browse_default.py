"""The view Loom opens on: every card with a cover, every medium on page one.

Measured on the full build: the Browse tab opened on music, and music had no
pictures at all, so the first impression was a wall of "no image". All domains
were ordered by one calibrated popularity -- but calibration puts sources of
*one domain* on one scale; a Deezer fan count and a MAL member count are not
two readings of anything, and the largest raw numbers simply won the page.

So the all-domains listing, which has no meaningful single order, leads with
works that have an image and interleaves the domains by each one's own
popularity rank. A listing of one domain keeps its plain popularity order.
"""

from __future__ import annotations

import pytest

from recsys.db import connect, init_db, query_items, upsert_items
from recsys.link import rebuild_links
from recsys.popularity import rebuild_popularity
from recsys.relations import rebuild_relations
from recsys.schemas import UnifiedItem
from recsys.works import rebuild_works


def work(domain, n, popularity, image=True):
    return UnifiedItem(
        domain=domain, source="src", source_id=str(n), title=f"{domain} {n}",
        popularity=float(popularity),
        image_url=f"https://img.example/{domain}/{n}.jpg" if image else None,
    )


CATALOG = [
    # Music's audience counts dwarf everyone's, and none of it has a picture.
    *[work("music", n, 10_000_000 - n, image=False) for n in range(10)],
    *[work("anime", n, 3_000_000 - n * 1000) for n in range(5)],
    *[work("book", n, 4_000_000 - n * 1000) for n in range(5)],
    *[work("movie", n, 30_000 - n) for n in range(5)],
    *[work("tv", n, 9_000 - n) for n in range(5)],
    work("manga", 0, 200_000, image=False),
    work("manga", 1, 100_000),
]


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "browse.db"
    init_db(path)
    c = connect(path)
    upsert_items(c, CATALOG)
    rebuild_links(c)
    rebuild_popularity(c)
    rebuild_relations(c)
    rebuild_works(c)
    yield c
    c.close()


def test_the_default_view_opens_on_works_with_covers(conn):
    _, page = query_items(conn, limit=20)
    assert all(i.image_url for i in page)


def test_every_medium_with_covers_is_on_the_first_page(conn):
    _, page = query_items(conn, limit=5)
    assert {i.domain for i in page} == {"anime", "book", "manga", "movie", "tv"}


def test_each_domain_keeps_its_own_popularity_order_inside_the_interleave(conn):
    _, page = query_items(conn, limit=25)
    for domain in ("anime", "book", "movie", "tv"):
        titles = [i.title for i in page if i.domain == domain]
        assert titles == [f"{domain} {n}" for n in range(len(titles))]


def test_works_without_a_cover_still_list_after_those_with_one(conn):
    total, everything = query_items(conn, limit=200)
    assert total == len(CATALOG)
    with_image = [bool(i.image_url) for i in everything]
    assert with_image == sorted(with_image, reverse=True)


def test_a_domain_listing_keeps_its_plain_popularity_order(conn):
    """Asked for music, you get the most popular music, picture or not."""
    _, page = query_items(conn, domain="manga")
    assert [i.title for i in page] == ["manga 0", "manga 1"]


def test_a_cover_lent_by_a_sibling_counts(tmp_path):
    """The representative may carry no image while another row of its work does
    -- an Open Library book whose cover is on the goodbooks row."""
    path = tmp_path / "lent.db"
    init_db(path)
    c = connect(path)
    upsert_items(c, [
        UnifiedItem(domain="book", source="openlibrary", source_id="1", title="Lent",
                    description="Prose.", external_ids={"isbn": "1"}),
        UnifiedItem(domain="book", source="goodbooks", source_id="1", title="Lent",
                    popularity=5.0, image_url="https://img.example/lent.jpg",
                    external_ids={"isbn": "1"}),
        UnifiedItem(domain="music", source="src", source_id="9", title="Loud",
                    popularity=9e9),
    ])
    rebuild_links(c)
    rebuild_popularity(c)
    rebuild_relations(c)
    rebuild_works(c)
    assert [i.title for i in query_items(c)[1]] == ["Lent", "Loud"]
    c.close()


def test_without_the_read_view_the_catalog_still_lists(tmp_path):
    path = tmp_path / "fresh.db"
    init_db(path)
    c = connect(path)
    upsert_items(c, CATALOG)
    assert query_items(c, limit=500)[0] == len(CATALOG)
    c.close()
