"""For you takes the same filters as Browse: tags, years, a minimum score, a sort.

A filter narrows what the profile is allowed to answer with; it never changes
the profile. And it is applied while the page fills, not to a finished page,
so asking for "8 and up" gets k works that are 8 and up rather than whichever
of the top k happened to be.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from recsys.api.routes import _load_collaborative_index, _load_similarity_index
from recsys.config import get_settings
from recsys.db import connect, init_db, set_rating, upsert_items
from recsys.link import rebuild_links
from recsys.relations import rebuild_relations
from recsys.schemas import UnifiedItem, utcnow
from recsys.works import rebuild_works
from tests.test_similar_route import write_matrix


def item(source_id, title, *, year, score, tags, domain="anime"):
    return UnifiedItem(
        domain=domain, source="anilist", source_id=str(source_id), title=title,
        year=year, mean_rating=score, n_ratings=1000, tags=tags, fetched_at=utcnow(),
        raw_json={"id": source_id, "relations": {"edges": []}},
    )


#: 1 is rated; 2..6 are ranked by closeness in that order.
SEED = [
    item(1, "Rated", year=2010, score=9.0, tags=["sci-fi"]),
    item(2, "Nearest", year=2011, score=6.0, tags=["sci-fi"]),
    item(3, "Second", year=1998, score=8.4, tags=["sci-fi", "time travel"]),
    item(4, "Third", year=2020, score=7.96, tags=["romance"]),
    item(5, "Fourth", year=2005, score=9.1, tags=["time travel"]),
    item(6, "A Book", year=2015, score=8.8, tags=["time travel"], domain="book"),
]

VECTORS = {
    "anime:anilist:1": [1.00, 0.00],
    "anime:anilist:2": [0.99, 0.14],
    "anime:anilist:3": [0.95, 0.31],
    "anime:anilist:4": [0.90, 0.44],
    "anime:anilist:5": [0.80, 0.60],
    "book:anilist:6": [0.70, 0.71],
}


@pytest.fixture(autouse=True)
def _clear_index_cache():
    _load_similarity_index.cache_clear()
    _load_collaborative_index.cache_clear()
    yield
    _load_similarity_index.cache_clear()
    _load_collaborative_index.cache_clear()


@pytest.fixture(params=["read view", "live joins"])
def client(request):
    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings.db_path)
    conn = connect(settings.db_path)
    upsert_items(conn, SEED)
    rebuild_links(conn)
    rebuild_relations(conn)
    if request.param == "read view":
        rebuild_works(conn)
    set_rating(conn, "anime:anilist:1", 9.0)
    write_matrix(settings.embeddings_path, VECTORS)
    conn.close()

    from recsys.api.server import create_app

    with TestClient(create_app()) as c:
        yield c


def titles(client, query=""):
    r = client.get(f"/api/recommend?k=10{query}")
    assert r.status_code == 200, r.text
    return [n["item"]["title"] for n in r.json()["neighbours"]]


def test_unfiltered_is_ranked_by_taste(client):
    assert titles(client) == ["Nearest", "Second", "Third", "Fourth", "A Book"]


def test_a_tag_keeps_only_works_carrying_it(client):
    assert titles(client, "&tag=time travel") == ["Second", "Fourth", "A Book"]


def test_several_tags_narrow(client):
    assert titles(client, "&tag=sci-fi&tag=time travel") == ["Second"]


def test_a_year_range(client):
    assert titles(client, "&year_min=2000&year_max=2012") == ["Nearest", "Fourth"]


def test_a_minimum_score_means_the_number_on_the_card(client):
    """7.96 prints as 8.0, so it passes "8 and up", as it does in Browse."""
    assert titles(client, "&min_rating=8") == ["Second", "Third", "Fourth", "A Book"]


def test_filters_combine_with_domains(client):
    assert titles(client, "&min_rating=8&domain=anime") == ["Second", "Third", "Fourth"]


def test_a_filtered_page_is_still_full(client):
    """k=2 with a filter that rejects the two nearest: the page fills from
    further down rather than coming back empty."""
    r = client.get("/api/recommend?k=2&tag=time travel").json()
    assert [n["item"]["title"] for n in r["neighbours"]] == ["Second", "Fourth"]


@pytest.mark.parametrize(("sort", "expected"), [
    ("rating", ["Fourth", "A Book", "Second", "Third", "Nearest"]),
    ("newest", ["Third", "A Book", "Nearest", "Fourth", "Second"]),
    ("oldest", ["Second", "Fourth", "Nearest", "A Book", "Third"]),
    ("title", ["A Book", "Fourth", "Nearest", "Second", "Third"]),
])
def test_a_sort_reorders_what_the_profile_chose(client, sort, expected):
    assert titles(client, f"&sort={sort}") == expected


def test_an_unknown_sort_is_422(client):
    assert client.get("/api/recommend?sort=vibes").status_code == 422
