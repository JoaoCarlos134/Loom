"""``/api/similar`` -- the only route that reads the model rather than the catalog.

Driven by a deterministic fake matrix written straight to parquet, so no test
downloads an embedder or depends on what a real one thinks two shows have in
common. What is being tested is the route's contract: what it does when the
matrix is missing, when it is stale, and when a neighbour is the query's own
sequel.
"""

from __future__ import annotations

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from recsys.api.routes import _load_collaborative_index, _load_similarity_index
from recsys.config import get_settings
from recsys.db import connect, init_db, upsert_items
from recsys.relations import rebuild_relations
from recsys.schemas import UnifiedItem, utcnow


def item(source_id, title, *, domain="anime", edges=()):
    return UnifiedItem(
        domain=domain, source="anilist", source_id=str(source_id), title=title,
        fetched_at=utcnow(),
        raw_json={"id": source_id, "relations": {"edges": list(edges)}},
    )


def edge(node_id, relation, node_type="ANIME"):
    return {"relationType": relation, "node": {"id": node_id, "type": node_type}}


#: 1 is the query. 2 is its sequel and the *nearest* neighbour -- which is the
#: whole failure the suppression exists for. 3 is an unrelated anime, 4 a book.
SEED = [
    item(1, "Steins;Gate", edges=[edge(2, "SEQUEL")]),
    item(2, "Steins;Gate 0"),
    item(3, "Monster"),
    item(4, "A Novel", domain="book"),
]

#: Hand-written vectors, so "nearest" is a fact of the fixture, not of a model.
VECTORS = {
    "anime:anilist:1": [1.0, 0.0],
    "anime:anilist:2": [0.99, 0.14],   # nearest to 1, and its sequel
    "anime:anilist:3": [0.80, 0.60],
    "book:anilist:4": [0.60, 0.80],
}


def write_matrix(path, vectors):
    uids = list(vectors)
    block = np.asarray([vectors[u] for u in uids], dtype="float32")
    block /= np.linalg.norm(block, axis=1, keepdims=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({
        "item_uid": pa.array(uids, type=pa.string()),
        "domain": pa.array([u.split(":")[0] for u in uids], type=pa.string()),
        "vector": pa.FixedSizeListArray.from_arrays(
            pa.array(block.reshape(-1), type=pa.float32()), block.shape[1]
        ),
    }), path)


@pytest.fixture(autouse=True)
def _clear_index_cache():
    # The loader is lru_cached on the path; a per-test DATA_DIR reuses paths.
    _load_similarity_index.cache_clear()
    _load_collaborative_index.cache_clear()
    yield
    _load_similarity_index.cache_clear()
    _load_collaborative_index.cache_clear()


@pytest.fixture
def seeded():
    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings.db_path)
    conn = connect(settings.db_path)
    upsert_items(conn, SEED)
    rebuild_relations(conn)
    conn.close()
    return settings


@pytest.fixture
def client(seeded):
    write_matrix(seeded.embeddings_path, VECTORS)
    from recsys.api.server import create_app

    with TestClient(create_app()) as c:
        yield c


def test_neighbours_come_back_ranked(client):
    body = client.get("/api/similar/anime:anilist:1").json()
    scores = [n["score"] for n in body["neighbours"]]
    assert scores == sorted(scores, reverse=True)


def test_a_neighbour_carries_the_catalog_row(client):
    """So the UI needs no second request per result."""
    first = client.get("/api/similar/anime:anilist:1").json()["neighbours"][0]
    assert first["item"]["title"] and first["item"]["domain"]


def test_the_query_is_never_its_own_neighbour(client):
    body = client.get("/api/similar/anime:anilist:1").json()
    assert all(n["item"]["item_uid"] != "anime:anilist:1" for n in body["neighbours"])


def test_a_works_own_sequel_is_suppressed(client):
    """The nearest neighbour to Steins;Gate is Steins;Gate 0. Correct, and not
    a recommendation -- it is the roadmap's own named failure of the model."""
    body = client.get("/api/similar/anime:anilist:1").json()
    assert "anime:anilist:2" not in [n["item"]["item_uid"] for n in body["neighbours"]]
    assert body["n_suppressed"] == 1


def test_the_franchise_can_be_asked_for_explicitly(client):
    body = client.get("/api/similar/anime:anilist:1?same_franchise=true").json()
    assert body["neighbours"][0]["item"]["item_uid"] == "anime:anilist:2"
    assert body["n_suppressed"] == 0


def test_suppression_does_not_shorten_the_page(client):
    """Over-fetching by the suppression count is what keeps k honest."""
    body = client.get("/api/similar/anime:anilist:1?k=2").json()
    assert len(body["neighbours"]) == 2


def test_asking_for_another_domain_is_the_whole_point(client):
    body = client.get("/api/similar/anime:anilist:1?domain=book").json()
    assert [n["item"]["domain"] for n in body["neighbours"]] == ["book"]


def test_an_unknown_domain_is_422(client):
    assert client.get("/api/similar/anime:anilist:1?domain=film").status_code == 422


def test_an_item_not_in_the_catalog_is_404(client):
    r = client.get("/api/similar/anime:anilist:999")
    assert r.status_code == 404


def test_an_item_missing_from_a_stale_matrix_is_409_not_404(seeded):
    """It is in the catalog; the matrix is simply older than the catalog. That
    is a rebuild instruction, not a missing item, and the status says so."""
    write_matrix(seeded.embeddings_path, {k: v for k, v in VECTORS.items()
                                          if k != "anime:anilist:3"})
    from recsys.api.server import create_app

    with TestClient(create_app()) as c:
        r = c.get("/api/similar/anime:anilist:3")
    assert r.status_code == 409
    assert "embed.py" in r.json()["detail"]


def test_no_matrix_at_all_is_503_naming_the_fix(seeded):
    from recsys.api.server import create_app

    with TestClient(create_app()) as c:
        r = c.get("/api/similar/anime:anilist:1")
    assert r.status_code == 503
    assert "embed.py" in r.json()["detail"]


def test_the_route_does_not_shadow_items(client):
    """Both use a :path converter under /api; a bad order would swallow one."""
    assert client.get("/api/items/anime:anilist:1").json()["title"] == "Steins;Gate"


# ---- method=cf -----------------------------------------------------------

#: The collaborative model in this fixture disagrees with the embeddings on
#: purpose: it puts 3 nearest to 1, where the vectors put 2 there. A test that
#: cannot tell the two models apart is not testing the switch.
CF_NEIGHBOURS = [
    ("anime:anilist:1", "anime:anilist:3", 0.91),
    ("anime:anilist:1", "anime:anilist:2", 0.44),
    ("anime:anilist:3", "anime:anilist:1", 0.91),
]


def write_cf(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({
        "item_uid": pa.array([r[0] for r in rows], type=pa.string()),
        "neighbour_uid": pa.array([r[1] for r in rows], type=pa.string()),
        "score": pa.array([r[2] for r in rows], type=pa.float32()),
    }), path)


@pytest.fixture
def cf_client(seeded):
    write_matrix(seeded.embeddings_path, VECTORS)
    write_cf(seeded.collaborative_path, CF_NEIGHBOURS)
    from recsys.api.server import create_app

    with TestClient(create_app()) as c:
        yield c


def test_cf_answers_from_the_ratings_model_not_the_vectors(cf_client):
    body = cf_client.get("/api/similar/anime:anilist:1?method=cf").json()
    assert next(n["item"]["item_uid"] for n in body["neighbours"]) == "anime:anilist:3"


def test_content_still_answers_from_the_vectors(cf_client):
    """Same fixture, other method — the two must not have collapsed into one."""
    body = cf_client.get("/api/similar/anime:anilist:1?same_franchise=true").json()
    assert body["neighbours"][0]["item"]["item_uid"] == "anime:anilist:2"


def test_the_response_says_which_model_answered(cf_client):
    assert cf_client.get("/api/similar/anime:anilist:1?method=cf").json()["method"] == "cf"
    assert cf_client.get("/api/similar/anime:anilist:1").json()["method"] == "content"


def test_franchise_suppression_applies_to_cf_too(cf_client):
    """CF loves a sequel even more than the embeddings do."""
    body = cf_client.get("/api/similar/anime:anilist:1?method=cf").json()
    assert "anime:anilist:2" not in [n["item"]["item_uid"] for n in body["neighbours"]]
    assert body["n_suppressed"] == 1


def test_a_work_nobody_rated_is_an_empty_answer_not_an_error(cf_client):
    """Unlike the embeddings, having no opinion is a normal CF outcome."""
    r = cf_client.get("/api/similar/book:anilist:4?method=cf")
    assert r.status_code == 200
    assert r.json()["neighbours"] == []


def test_an_unknown_method_is_422(cf_client):
    r = cf_client.get("/api/similar/anime:anilist:1?method=magic")
    assert r.status_code == 422
    assert "magic" in r.json()["detail"]


def test_no_cf_model_is_503_naming_the_fix(seeded):
    write_matrix(seeded.embeddings_path, VECTORS)  # content works, cf does not
    from recsys.api.server import create_app

    with TestClient(create_app()) as c:
        r = c.get("/api/similar/anime:anilist:1?method=cf")
    assert r.status_code == 503
    assert "cf.py" in r.json()["detail"]


def test_a_model_built_while_the_app_runs_is_picked_up(seeded):
    """The 503 says to run embed.py / cf.py; doing so must not need a restart.

    A missing file is the one answer the loader must not remember.
    """
    from recsys.api.server import create_app

    with TestClient(create_app()) as c:
        assert c.get("/api/similar/anime:anilist:1").status_code == 503
        assert c.get("/api/similar/anime:anilist:1?method=cf").status_code == 503

        write_matrix(seeded.embeddings_path, VECTORS)
        write_cf(seeded.collaborative_path, CF_NEIGHBOURS)

        assert c.get("/api/similar/anime:anilist:1").status_code == 200
        assert c.get("/api/similar/anime:anilist:1?method=cf").status_code == 200
