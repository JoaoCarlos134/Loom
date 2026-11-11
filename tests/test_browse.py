"""Browsing by more than popularity: tags, year, score, order, and page size.

Filters and orders are asked twice, once of the live joins and once of the read
view (the ``either`` fixture), because ``works`` is a cache and the rule for a
cache is that it returns the answers of the thing it caches. What only the view
can do -- weigh a score by its votes, fill a year from a sibling row, feed the
tag picker -- is tested against the view alone, and says so.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from recsys.config import get_settings
from recsys.db import (
    ITEM_SORTS,
    connect,
    get_item,
    init_db,
    query_items,
    top_tags,
    upsert_items,
)
from recsys.link import rebuild_links
from recsys.schemas import UnifiedItem, utcnow
from recsys.works import rebuild_works


def item(source_id, title, *, domain="anime", source="jikan", **kw):
    return UnifiedItem(
        domain=domain, source=source, source_id=str(source_id), title=title,
        fetched_at=utcnow(), **kw,
    )


CATALOG = [
    item(1, "Cowboy Bebop", popularity=300.0, year=1998, mean_rating=8.8, n_ratings=900,
         tags=["space", "action", "jazz"]),
    item(2, "Steins;Gate", popularity=900.0, year=2011, mean_rating=9.1, n_ratings=1200,
         tags=["time travel", "sci-fi"]),
    item(3, "Monster", popularity=100.0, year=2004, mean_rating=8.9, n_ratings=400,
         tags=["mystery", "action"]),
    item(4, "Undated", popularity=50.0, tags=["action"]),
    item(5, "A Novel", domain="book", source="goodbooks", popularity=500.0, year=1960,
         mean_rating=7.0, n_ratings=5000, tags=["time travel", "romance"]),
]


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "b.db")
    c = connect(tmp_path / "b.db")
    upsert_items(c, CATALOG)
    yield c
    c.close()


@pytest.fixture(params=["live joins", "read view"])
def either(request, conn):
    """The same catalog down both read paths."""
    if request.param == "read view":
        rebuild_works(conn)
    return conn


def titles(result):
    return [i.title for i in result[1]]


# ---- filters -------------------------------------------------------------


def test_a_tag_filters(either):
    assert sorted(titles(query_items(either, tags=["time travel"]))) == [
        "A Novel", "Steins;Gate",
    ]


def test_several_tags_must_all_be_carried(either):
    """A second tag narrows. Anyone adding "sci-fi" to "time travel" wants
    fewer works, and OR would hand them more."""
    assert titles(query_items(either, tags=["time travel", "sci-fi"])) == ["Steins;Gate"]


def test_a_tag_nothing_carries_is_an_empty_page_not_an_error(either):
    assert query_items(either, tags=["no such tag"]) == (0, [])


def test_tags_combine_with_the_domain(either):
    assert titles(query_items(either, tags=["time travel"], domain="book")) == ["A Novel"]


def test_several_domains_can_be_asked_for_at_once(either):
    """Anime and books, not the rest -- which one domain at a time cannot say."""
    upsert_items(either, [item(9, "A Film", domain="movie", source="tmdb", popularity=1.0)])
    if either.execute("SELECT EXISTS(SELECT 1 FROM works)").fetchone()[0]:
        rebuild_works(either)
    total, page = query_items(either, domain=["anime", "book"])
    assert total == 5 and {i.domain for i in page} == {"anime", "book"}
    assert query_items(either, domain=["movie"])[0] == 1
    assert query_items(either, domain=[])[0] == 6       # none named is all of them


def test_tags_combine_with_a_search(either):
    assert titles(query_items(either, q="monster", tags=["action"])) == ["Monster"]
    assert query_items(either, q="monster", tags=["jazz"]) == (0, [])


def test_year_bounds_are_inclusive(either):
    assert sorted(titles(query_items(either, year_min=1998, year_max=2004))) == [
        "Cowboy Bebop", "Monster",
    ]


def test_a_work_with_no_year_never_passes_a_year_filter(either):
    """A filter is a claim about the work; an absent value supports none."""
    assert "Undated" not in titles(query_items(either, year_min=1000))
    assert "Undated" not in titles(query_items(either, year_max=2100))


def test_a_minimum_score_filters_on_the_score_shown(either):
    assert sorted(titles(query_items(either, min_rating=8.9))) == ["Monster", "Steins;Gate"]


def test_the_total_counts_what_matched_not_the_page(either):
    total, page = query_items(either, tags=["action"], limit=1)
    assert total == 3 and len(page) == 1


def test_a_filtered_listing_pages(either):
    first = query_items(either, tags=["action"], limit=2, offset=0)[1]
    rest = query_items(either, tags=["action"], limit=2, offset=2)[1]
    assert len(first) == 2 and len(rest) == 1
    assert {i.item_uid for i in first}.isdisjoint(i.item_uid for i in rest)


# ---- order ---------------------------------------------------------------


def test_popularity_is_still_the_default(either):
    assert titles(query_items(either))[:2] == ["Steins;Gate", "A Novel"]


def test_newest_and_oldest(either):
    assert titles(query_items(either, sort="newest"))[:2] == ["Steins;Gate", "Monster"]
    assert titles(query_items(either, sort="oldest"))[:2] == ["A Novel", "Cowboy Bebop"]


def test_an_undated_work_sorts_last_either_way(either):
    """SQLite puts NULL first ascending. An undated work is not the oldest."""
    assert titles(query_items(either, sort="newest"))[-1] == "Undated"
    assert titles(query_items(either, sort="oldest"))[-1] == "Undated"


def test_title_order_ignores_case(conn):
    upsert_items(conn, [item(6, "akira", popularity=1.0)])
    rebuild_works(conn)
    assert titles(query_items(conn, sort="title"))[:2] == ["A Novel", "akira"]


def test_a_search_is_ranked_by_relevance_unless_told_otherwise(either):
    upsert_items(either, [item(7, "Monster Musume", popularity=999.0, year=2015)])
    if either.execute("SELECT EXISTS(SELECT 1 FROM works)").fetchone()[0]:
        rebuild_works(either)
    assert titles(query_items(either, q="monster", sort="newest"))[0] == "Monster Musume"
    assert titles(query_items(either, q="monster", sort="oldest"))[0] == "Monster"


def test_an_unknown_order_is_refused(either):
    with pytest.raises(ValueError, match="unknown sort"):
        query_items(either, sort="vibes")


def test_every_advertised_order_runs(either):
    for sort in ITEM_SORTS:
        assert query_items(either, sort=sort)[0] == len(CATALOG)


# ---- highest rated -------------------------------------------------------


def test_one_vote_of_ten_does_not_top_the_chart(conn):
    """The catalog's best work, by raw mean, was a road-safety short with one
    vote. Sorted by the weighted key it sinks to the domain's average -- which
    takes a domain with an average, hence the unremarkable shows beside it."""
    upsert_items(conn, [item(8, "Road Safety Short", mean_rating=10.0, n_ratings=1)])
    upsert_items(conn, [
        item(100 + n, f"Unremarkable {n}", mean_rating=6.0, n_ratings=1000) for n in range(4)
    ])
    rebuild_works(conn)
    ranked = titles(query_items(conn, sort="rating", domain="anime"))
    assert ranked[0] == "Steins;Gate"
    assert ranked.index("Road Safety Short") > ranked.index("Monster")


def test_the_weighting_does_not_change_the_score_shown_or_filtered_on(conn):
    """A "9 and up" filter has to mean the number printed on the card."""
    upsert_items(conn, [item(8, "Road Safety Short", mean_rating=10.0, n_ratings=1)])
    rebuild_works(conn)
    found = query_items(conn, min_rating=9.5)[1]
    assert [(i.title, i.mean_rating) for i in found] == [("Road Safety Short", 10.0)]


def test_the_weight_is_set_per_domain(conn):
    """Half the rated TV shows have under 77 votes and half the rated books
    over 21,157; one constant would ignore every show or believe every book."""
    rebuild_works(conn)
    keys = dict(conn.execute("SELECT item_uid, rating_key FROM works"))
    # The only rated book is its domain's whole evidence, so its key is its mean.
    assert keys["book:goodbooks:5"] == pytest.approx(7.0)
    # Anime's median vote count is 900: Bebop's own count, so it sits halfway
    # between its mean and the domain's.
    anime_mean = (8.8 + 9.1 + 8.9) / 3
    assert keys["anime:jikan:1"] == pytest.approx((8.8 + anime_mean) / 2)


def test_a_score_with_no_count_is_still_ordered_by_its_score(conn):
    """Unknown is not zero. Taken at zero votes every such work collapses onto
    the domain mean and they all tie, whatever they scored."""
    upsert_items(conn, [
        item(20, "Uncounted High", domain="tv", source="tmdb", mean_rating=9.0),
        item(21, "Uncounted Low", domain="tv", source="tmdb", mean_rating=5.0),
    ])
    rebuild_works(conn)
    assert titles(query_items(conn, sort="rating", domain="tv")) == [
        "Uncounted High", "Uncounted Low",
    ]


def test_missing_counts_do_not_drag_the_weight_to_nothing(conn):
    """The median is of the counts that exist. Counting a missing one as zero,
    three uncounted works here would make ``m`` one vote -- and one vote of
    ten would be believed again."""
    upsert_items(conn, [
        item(30 + n, f"Uncounted {n}", mean_rating=7.0) for n in range(3)
    ] + [item(8, "Road Safety Short", mean_rating=10.0, n_ratings=1)])
    rebuild_works(conn)
    ranked = titles(query_items(conn, sort="rating", domain="anime"))
    assert ranked.index("Road Safety Short") > ranked.index("Steins;Gate")


def test_a_work_nobody_scored_has_no_key_and_sorts_last(conn):
    rebuild_works(conn)
    assert conn.execute(
        "SELECT rating_key FROM works WHERE title = 'Undated'"
    ).fetchone()[0] is None
    assert titles(query_items(conn, sort="rating"))[-1] == "Undated"


# ---- the union ------------------------------------------------------------

#: One book, two rows. The text row represents it and has no year or score;
#: the ratings row has both. 8,399 of 10,000 book works look like this.
SPLIT_BOOK = [
    item("0439023483", "The Hunger Games", domain="book", source="openlibrary",
         description="Katniss volunteers.", tags=["dystopia"],
         external_ids={"isbn": "0439023483", "openlibrary_url": "https://openlibrary.org/x"}),
    item(2767052, "The Hunger Games", domain="book", source="goodbooks", year=2008,
         mean_rating=8.68, n_ratings=4_780_653, popularity=4_780_653.0, tags=["ya"],
         external_ids={"isbn": "0439023483",
                       "goodreads_url": "https://www.goodreads.com/book/show/2767052"}),
]


def test_the_view_filters_on_the_year_the_card_shows(conn):
    upsert_items(conn, SPLIT_BOOK)
    rebuild_links(conn)
    rebuild_works(conn)
    found = query_items(conn, year_min=2008, year_max=2008, min_rating=8.5)[1]
    assert [(i.item_uid, i.year, i.mean_rating) for i in found] == [
        ("book:openlibrary:0439023483", 2008, 8.68),
    ]


def test_a_tag_filter_finds_a_work_only_by_the_tags_its_card_shows(conn):
    """Lists are not merged across a work's rows anywhere on the read path."""
    upsert_items(conn, SPLIT_BOOK)
    rebuild_links(conn)
    rebuild_works(conn)
    assert titles(query_items(conn, tags=["dystopia"])) == ["The Hunger Games"]
    assert query_items(conn, tags=["ya"]) == (0, [])


def test_a_detail_view_carries_every_sources_links(conn):
    """The page's text came from Open Library; the Goodreads link is the other
    row's. Neither row's own ids are replaced."""
    upsert_items(conn, SPLIT_BOOK)
    rebuild_links(conn)
    ids = get_item(conn, "book:openlibrary:0439023483").external_ids
    assert ids["openlibrary_url"] == "https://openlibrary.org/x"
    assert ids["goodreads_url"] == "https://www.goodreads.com/book/show/2767052"
    assert ids["isbn"] == "0439023483"


def test_a_listing_does_not_pay_for_ids_it_never_shows(conn):
    upsert_items(conn, SPLIT_BOOK)
    rebuild_links(conn)
    listed = next(i for i in query_items(conn, domain="book")[1]
                  if i.title == "The Hunger Games")
    assert "goodreads_url" not in listed.external_ids


# ---- the tag picker ------------------------------------------------------


def test_the_picker_offers_the_most_carried_tags_first(conn):
    rebuild_works(conn)
    assert top_tags(conn, limit=2) == [("action", 3), ("time travel", 2)]


def test_the_picker_counts_within_a_domain(conn):
    rebuild_works(conn)
    assert top_tags(conn, domain="book") == [("romance", 1), ("time travel", 1)]


def test_the_picker_searches_inside_tag_names(conn):
    rebuild_works(conn)
    assert [t for t, _ in top_tags(conn, q="TRAV")] == ["time travel"]


def test_a_percent_sign_in_the_picker_is_not_a_wildcard(conn):
    rebuild_works(conn)
    assert top_tags(conn, q="%") == []


def test_the_picker_is_empty_until_the_view_is_built(conn):
    """The degradation: no suggestions, and typed tags still filter."""
    assert top_tags(conn) == []
    assert titles(query_items(conn, tags=["jazz"])) == ["Cowboy Bebop"]


def test_rebuilding_drops_tags_of_works_that_are_gone(conn):
    rebuild_works(conn)
    with conn:
        conn.execute("DELETE FROM items WHERE item_uid = 'anime:jikan:1'")
    rebuild_works(conn)
    assert top_tags(conn, q="jazz") == []
    assert conn.execute("SELECT COUNT(*) FROM work_tags WHERE tag = 'jazz'").fetchone()[0] == 0


# ---- through the API -----------------------------------------------------


@pytest.fixture
def client():
    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings.db_path)
    c = connect(settings.db_path)
    upsert_items(c, CATALOG)
    rebuild_works(c)
    c.close()
    from recsys.api.server import create_app

    with TestClient(create_app()) as tc:
        yield tc


def api_titles(response):
    return [i["title"] for i in response.json()["items"]]


def test_the_api_takes_repeated_tags(client):
    r = client.get("/api/items", params=[("tag", "time travel"), ("tag", "sci-fi")])
    assert api_titles(r) == ["Steins;Gate"]


def test_the_api_folds_tag_case_the_way_the_schema_stored_it(client):
    assert api_titles(client.get("/api/items?tag=Sci-Fi")) == ["Steins;Gate"]


def test_the_api_takes_year_and_score_bounds(client):
    r = client.get("/api/items?year_min=2000&year_max=2010&min_rating=8")
    assert api_titles(r) == ["Monster"]


def test_the_api_sorts(client):
    assert api_titles(client.get("/api/items?sort=oldest"))[0] == "A Novel"


def test_the_api_refuses_an_unknown_sort(client):
    r = client.get("/api/items?sort=vibes")
    assert r.status_code == 422 and "vibes" in r.json()["detail"]


def test_the_api_takes_repeated_domains(client):
    both = client.get("/api/items", params=[("domain", "anime"), ("domain", "book")]).json()
    assert both["total"] == 5
    assert client.get("/api/items?domain=book").json()["total"] == 1
    # "all", an empty value, or every domain named: no restriction.
    assert client.get("/api/items?domain=all").json()["total"] == 5
    assert client.get("/api/items?domain=").json()["total"] == 5
    r = client.get("/api/items", params=[("domain", "anime"), ("domain", "poetry")])
    assert r.status_code == 422 and "poetry" in r.json()["detail"]


def test_the_tag_picker_counts_across_the_chosen_domains(client):
    r = client.get("/api/tags", params=[("domain", "anime"), ("domain", "book"), ("q", "time")])
    assert r.json() == [{"tag": "time travel", "n": 2}]


def test_the_api_refuses_an_absurd_number_of_tags(client):
    """Each is a subquery; past SQLite's expression depth that was a 500."""
    r = client.get("/api/items", params=[("tag", f"t{n}") for n in range(21)])
    assert r.status_code == 422 and "tags" in r.json()["detail"]


def test_the_api_refuses_an_impossible_year(client):
    assert client.get("/api/items?year_min=12").status_code == 422


def test_the_page_size_is_the_callers_to_choose(client):
    assert len(client.get("/api/items?limit=2").json()["items"]) == 2
    assert client.get("/api/items?limit=200").status_code == 200
    assert client.get("/api/items?limit=201").status_code == 422


def test_the_tags_route(client):
    assert client.get("/api/tags?limit=1").json() == [{"tag": "action", "n": 3}]
    assert client.get("/api/tags?domain=book&q=rom").json() == [{"tag": "romance", "n": 1}]
    assert client.get("/api/tags?domain=poetry").status_code == 422


def test_the_library_can_be_searched_and_sorted(client):
    for uid, score in (("anime:jikan:1", 6), ("anime:jikan:2", 9), ("anime:jikan:3", 7)):
        assert client.put(f"/api/ratings/{uid}", json={"rating": score}).status_code == 200
        # Windows' clock ticks every ~15 ms; three ratings inside one tick tie
        # on rated_at and "most recent first" falls back to title order.
        time.sleep(0.02)

    def lib(query=""):
        return [r["item"]["title"] for r in client.get(f"/api/library{query}").json()["items"]]

    assert lib("?sort=rating") == ["Steins;Gate", "Monster", "Cowboy Bebop"]
    assert lib("?sort=title") == ["Cowboy Bebop", "Monster", "Steins;Gate"]
    assert lib() == ["Monster", "Steins;Gate", "Cowboy Bebop"]   # most recent first
    assert lib("?q=bebop") == ["Cowboy Bebop"]
    assert lib("?q=!!!") == []
    assert client.get("/api/library?sort=vibes").status_code == 422
