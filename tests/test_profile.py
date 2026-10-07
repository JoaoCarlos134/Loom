"""The taste vector, and ``/api/recommend`` on top of it.

Vectors are hand-written so "nearest my taste" is a fact of the fixture rather
than of whatever a real embedder happens to think. What is under test is the
one rule the profile has -- count what you rated at or above your own mean --
and the cases a threshold usually needs a special case for.
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from recsys.api.routes import _load_collaborative_index, _load_similarity_index
from recsys.config import get_settings
from recsys.db import connect, init_db, set_rating, upsert_items
from recsys.link import rebuild_links
from recsys.models.profile import build_profile
from recsys.models.similarity import SimilarityIndex
from recsys.relations import rebuild_relations
from recsys.schemas import UnifiedItem, utcnow
from tests.test_similar_route import write_matrix


def item(source_id, title, *, domain="anime", edges=()):
    return UnifiedItem(
        domain=domain, source="anilist", source_id=str(source_id), title=title,
        fetched_at=utcnow(),
        raw_json={"id": source_id, "relations": {"edges": list(edges)}},
    )


def edge(node_id, relation, node_type="ANIME"):
    return {"relationType": relation, "node": {"id": node_id, "type": node_type}}


def index_of(vectors) -> SimilarityIndex:
    block = np.asarray(list(vectors.values()), dtype="float32")
    block /= np.linalg.norm(block, axis=1, keepdims=True)
    uids = list(vectors)
    return SimilarityIndex(uids, [u.split(":")[0] for u in uids], block)


# ---- the rule ------------------------------------------------------------


def test_one_rating_is_its_own_mean_and_makes_a_profile():
    """The case a fixed bar or an ``n >= 2`` guard would fail: a new library
    holds exactly one thing, which is when a recommendation is most wanted."""
    index = index_of({"anime:anilist:1": [1.0, 0.0]})
    profile = build_profile(index, {"anime:anilist:1": 6.0})

    assert profile is not None
    assert profile.n_rated == 1 and profile.n_used == 1
    assert np.allclose(profile.vector, [1.0, 0.0])


def test_rating_everything_the_same_counts_everything():
    """All at the mean, so ``>=`` keeps them. A strict ``>`` would keep none,
    and the profile would vanish for the most agreeable user there is."""
    index = index_of({"anime:anilist:1": [1.0, 0.0], "anime:anilist:2": [0.0, 1.0]})
    profile = build_profile(index, {"anime:anilist:1": 8.0, "anime:anilist:2": 8.0})

    assert profile is not None and profile.n_used == 2


def test_what_you_rated_below_your_own_mean_is_left_out():
    index = index_of({"anime:anilist:1": [1.0, 0.0], "anime:anilist:2": [0.0, 1.0]})
    profile = build_profile(index, {"anime:anilist:1": 10.0, "anime:anilist:2": 2.0})

    assert profile.n_used == 1
    assert np.allclose(profile.vector, [1.0, 0.0])


def test_the_bar_is_your_mean_not_the_middle_of_the_scale():
    """Rating three things 10, 9 and 8 does not mean you liked all three. A
    fixed midpoint would keep all of them; your own mean of 9 keeps two."""
    index = index_of({
        "anime:anilist:1": [1.0, 0.0],
        "anime:anilist:2": [0.0, 1.0],
        "anime:anilist:3": [-1.0, 0.0],
    })
    profile = build_profile(index, {
        "anime:anilist:1": 10.0, "anime:anilist:2": 9.0, "anime:anilist:3": 8.0,
    })
    assert profile.n_used == 2


def test_the_profile_is_a_unit_vector():
    """The matrix is queried by dot product and its rows are normalised, so a
    profile of any other length would score on magnitude, not direction."""
    index = index_of({"anime:anilist:1": [3.0, 4.0], "anime:anilist:2": [1.0, 0.0]})
    profile = build_profile(index, {"anime:anilist:1": 9.0, "anime:anilist:2": 9.0})

    assert float(np.linalg.norm(profile.vector)) == pytest.approx(1.0)


def test_no_ratings_is_no_profile():
    assert build_profile(index_of({"anime:anilist:1": [1.0, 0.0]}), {}) is None


def test_ratings_the_matrix_has_never_seen_are_no_profile():
    """A catalog rated after the last embed run. Distinct from having no
    ratings, because the fix is to rebuild the matrix, not to rate something."""
    index = index_of({"anime:anilist:1": [1.0, 0.0]})
    assert build_profile(index, {"anime:anilist:999": 9.0}) is None


def test_n_rated_counts_ratings_the_matrix_lacks_and_n_used_does_not():
    """The gap is the caveat on the recommendation, so it is reported."""
    index = index_of({"anime:anilist:1": [1.0, 0.0]})
    profile = build_profile(index, {"anime:anilist:1": 9.0, "anime:anilist:999": 9.0})

    assert profile.n_rated == 2 and profile.n_used == 1


def test_a_centroid_that_cancels_to_zero_is_no_profile():
    index = index_of({"anime:anilist:1": [1.0, 0.0], "anime:anilist:2": [-1.0, 0.0]})
    assert build_profile(index, {"anime:anilist:1": 9.0, "anime:anilist:2": 9.0}) is None


# ---- the route -----------------------------------------------------------

#: 1 is rated. 2 is its sequel and sits nearest it. 3 is an unrelated anime.
#: 4 and 5 are books, 4 the closer -- so a cross-domain ask has a right answer.
SEED = [
    item(1, "Steins;Gate", edges=[edge(2, "SEQUEL")]),
    item(2, "Steins;Gate 0"),
    item(3, "Monster"),
    item(4, "Idoru", domain="book"),
    item(5, "A Cookbook", domain="book"),
]

VECTORS = {
    "anime:anilist:1": [1.0, 0.0],
    "anime:anilist:2": [0.99, 0.14],
    "anime:anilist:3": [0.80, 0.60],
    "book:anilist:4": [0.90, 0.44],
    "book:anilist:5": [-0.30, 0.95],
}


@pytest.fixture(autouse=True)
def _clear_index_cache():
    _load_similarity_index.cache_clear()
    _load_collaborative_index.cache_clear()
    yield
    _load_similarity_index.cache_clear()
    _load_collaborative_index.cache_clear()


@pytest.fixture
def conn():
    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings.db_path)
    c = connect(settings.db_path)
    upsert_items(c, SEED)
    rebuild_links(c)
    rebuild_relations(c)
    write_matrix(settings.embeddings_path, VECTORS)
    yield c
    c.close()


@pytest.fixture
def client(conn):
    from recsys.api.server import create_app

    with TestClient(create_app()) as c:
        yield c


def test_recommending_before_you_have_rated_anything_says_so(client):
    r = client.get("/api/recommend")
    assert r.status_code == 409
    assert "rate something" in r.json()["detail"]


def test_recommendations_come_back_ranked(client, conn):
    set_rating(conn, "anime:anilist:1", 9.0)
    body = client.get("/api/recommend").json()

    scores = [n["score"] for n in body["neighbours"]]
    assert scores == sorted(scores, reverse=True)
    assert body["n_rated"] == 1 and body["n_used"] == 1


def test_you_are_never_recommended_what_you_rated(client, conn):
    set_rating(conn, "anime:anilist:1", 9.0)
    body = client.get("/api/recommend").json()

    assert "anime:anilist:1" not in [n["item"]["item_uid"] for n in body["neighbours"]]


def test_the_sequel_of_something_you_rated_is_suppressed(client, conn):
    """The failure ``/api/similar`` suppresses, and worse here: a profile
    recommendation opening with the sequel to what you just rated is a table
    of contents, not a suggestion."""
    set_rating(conn, "anime:anilist:1", 9.0)
    body = client.get("/api/recommend").json()

    assert "anime:anilist:2" not in [n["item"]["item_uid"] for n in body["neighbours"]]
    assert body["n_suppressed"] == 1


def test_the_franchise_can_be_asked_for_explicitly(client, conn):
    set_rating(conn, "anime:anilist:1", 9.0)
    body = client.get("/api/recommend?same_franchise=true").json()

    assert "anime:anilist:2" in [n["item"]["item_uid"] for n in body["neighbours"]]
    assert body["n_suppressed"] == 0


def test_books_can_be_recommended_from_anime_ratings_alone(client, conn):
    """The cross-domain bridge, and the thing CF structurally cannot do here --
    every one of the 19.2M corpus ratings is anime."""
    set_rating(conn, "anime:anilist:1", 9.0)
    body = client.get("/api/recommend?domain=book").json()

    assert [n["item"]["item_uid"] for n in body["neighbours"]] == [
        "book:anilist:4", "book:anilist:5",
    ]
    assert all(n["item"]["domain"] == "book" for n in body["neighbours"])


def test_a_rating_on_a_row_the_matrix_lacks_is_reported_not_hidden(client, conn):
    """``n_used`` is how a caller learns the profile is thinner than the rating
    count suggests -- here because one rated row was never embedded."""
    upsert_items(conn, [UnifiedItem(
        domain="anime", source="mal", source_id="77", title="Unembedded", fetched_at=utcnow(),
    )])
    set_rating(conn, "anime:anilist:1", 9.0)
    set_rating(conn, "anime:mal:77", 9.0)

    body = client.get("/api/recommend").json()
    assert body["n_rated"] == 2 and body["n_used"] == 1


def test_recommend_rejects_an_unknown_domain(client, conn):
    set_rating(conn, "anime:anilist:1", 9.0)
    assert client.get("/api/recommend?domain=poetry").status_code == 422


def test_no_matrix_is_a_503_naming_the_script(conn):
    get_settings().embeddings_path.unlink()
    from recsys.api.server import create_app

    with TestClient(create_app()) as c:
        r = c.get("/api/recommend")
    assert r.status_code == 503
    assert "scripts/embed.py" in r.json()["detail"]
