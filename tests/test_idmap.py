"""Cross-domain twins: an anime and its own TMDB entry are one work.

TMDB files a lot of anime under television and film, so the catalog carried
*Steins;Gate* twice -- ``anime:anilist:9253`` and ``tv:tmdb:42509`` -- as two
unrelated works, and rating one recommended the other. ``link.py`` could not
join them: it links on ``external_ids.mal_id`` and TMDB rows carry no MAL id.

The fix keeps the linker's rule of exact ids and no title matching. A static
cross-reference -- Wikidata's MyAnimeList (P4086) <-> TMDB TV (P4983) / movie
(P4947) claims -- is loaded by a sibling contract into ``id_map``, and the
linker treats a mapped TMDB row as one more row of the anime's work.

The fixture is hand-written in the SPARQL 1.1 JSON results shape. Its ids are
illustrative; the tests depend on their shape, not on their truth.
"""

from __future__ import annotations

import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from recsys.api.routes import _load_collaborative_index, _load_similarity_index
from recsys.config import get_settings
from recsys.db import connect, count_items, domain_stats, init_db, query_items, upsert_items
from recsys.ingest.registry import available, get_ingester
from recsys.link import rebuild_links, representative_uid
from recsys.popularity import rebuild_popularity
from recsys.schemas import UnifiedItem
from recsys.works import rebuild_works
from tests.conftest import FIXTURES, load_fixture


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "idmap.db"
    init_db(path)
    c = connect(path)
    yield c
    c.close()


def anime(source, source_id, mal_id, *, title="Steins;Gate", tags=(), description=None,
          popularity=None):
    return UnifiedItem(
        domain="anime", source=source, source_id=str(source_id), title=title,
        tags=list(tags), description=description, popularity=popularity,
        external_ids={"mal_id": str(mal_id)},
    )


def tmdb(domain, tmdb_id, *, title="Steins;Gate", tags=(), description=None, popularity=None):
    return UnifiedItem(
        domain=domain, source="tmdb", source_id=str(tmdb_id), title=title,
        tags=list(tags), description=description, popularity=popularity,
        external_ids={"tmdb_id": str(tmdb_id), "imdb_id": "tt1910272"},
    )


def load_map(conn, monkeypatch, path=None):
    """Run the real loader against a saved answer, as bootstrap would."""
    import asyncio

    monkeypatch.setenv("WIKIDATA_IDMAP_PATH",
                       str(path or FIXTURES / "wikidata_mal_tmdb_sample.json"))
    get_settings.cache_clear()
    loader = get_ingester("idmap_wikidata", get_settings())
    return asyncio.run(loader.run(conn))


# ---- the loader -----------------------------------------------------------


def test_bindings_parse_to_exact_pairs_and_skip_the_malformed():
    from recsys.ingest.idmap_wikidata import parse_bindings

    rows = parse_bindings(load_fixture("wikidata_mal_tmdb_sample.json"))
    assert sorted(rows) == sorted([
        ("anime", "mal_id", "9253", "tv", "tmdb_id", "42509"),
        ("anime", "mal_id", "790", "tv", "tmdb_id", "31935"),
        ("anime", "mal_id", "199", "movie", "tmdb_id", "129"),
        ("anime", "mal_id", "16498", "tv", "tmdb_id", "1429"),
        ("anime", "mal_id", "25777", "tv", "tmdb_id", "1429"),
        ("anime", "mal_id", "777", "tv", "tmdb_id", "500"),
        ("anime", "mal_id", "777", "movie", "tmdb_id", "500"),
    ])


def test_the_loader_is_registered_like_any_other_runner():
    assert "idmap_wikidata" in available()


def test_the_loader_fills_id_map_and_records_its_run(conn, monkeypatch):
    result = load_map(conn, monkeypatch)
    assert result.status == "ok"
    assert result.n_inserted == 7
    assert conn.execute("SELECT COUNT(*) FROM id_map").fetchone()[0] == 7
    run = conn.execute("SELECT source, status FROM ingest_runs").fetchone()
    assert (run["source"], run["status"]) == ("wikidata", "ok")


def test_reloading_replaces_rather_than_accumulates(conn, monkeypatch, tmp_path):
    """A claim Wikidata has since withdrawn must not survive the next load."""
    load_map(conn, monkeypatch)
    payload = load_fixture("wikidata_mal_tmdb_sample.json")
    payload["results"]["bindings"] = [
        b for b in payload["results"]["bindings"]
        if (b.get("mal") or {}).get("value") != "790"
    ]
    smaller = tmp_path / "smaller.json"
    smaller.write_text(json.dumps(payload), encoding="utf-8")
    result = load_map(conn, monkeypatch, smaller)

    assert result.status == "ok"
    assert conn.execute("SELECT COUNT(*) FROM id_map WHERE value = '790'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM id_map").fetchone()[0] == 6


def test_an_answer_with_no_claims_fails_and_keeps_the_map(conn, monkeypatch, tmp_path):
    """Zero claims means a broken answer; replacing the map with it would
    unlink every twin on the next relink and report success."""
    load_map(conn, monkeypatch)
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"head": {}, "results": {"bindings": []}}), encoding="utf-8")
    result = load_map(conn, monkeypatch, empty)
    assert result.status == "error"
    assert conn.execute("SELECT COUNT(*) FROM id_map").fetchone()[0] == 7


def test_the_query_retries_a_throttled_answer_then_caches_it(conn):
    """One request on a cold cache, after one 429; none on a warm one."""
    import asyncio

    import httpx

    calls = []
    body = load_fixture("wikidata_mal_tmdb_sample.json")

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=body)

    loader = get_ingester("idmap_wikidata", get_settings())
    loader.transport = httpx.MockTransport(handler)
    assert asyncio.run(loader.run(conn)).status == "ok"
    assert len(calls) == 2
    assert calls[-1].headers["User-Agent"].startswith("Loom/")
    assert "P4086" in calls[-1].url.params["query"]

    again = get_ingester("idmap_wikidata", get_settings())
    again.transport = httpx.MockTransport(handler)
    result = asyncio.run(again.run(conn))
    assert (result.status, result.n_cached, len(calls)) == ("ok", 1, 2)


def test_a_missing_local_file_is_a_failed_run_not_a_crash(conn, monkeypatch, tmp_path):
    import asyncio

    monkeypatch.setenv("WIKIDATA_IDMAP_PATH", str(tmp_path / "nope.json"))
    get_settings.cache_clear()
    result = asyncio.run(get_ingester("idmap_wikidata", get_settings()).run(conn))
    assert result.status == "error"
    assert "nope.json" in (result.error or "")


# ---- the linker -----------------------------------------------------------


def test_an_anime_and_its_tmdb_twin_are_one_work(conn, monkeypatch):
    """The bug: two works where there is one, so rating one recommends the other."""
    upsert_items(conn, [
        anime("anilist", 9253, 9253, description="A lab of self-styled scientists."),
        anime("mal", 9253, 9253),
        tmdb("tv", 42509, description="Rintaro Okabe discovers time travel."),
    ])
    load_map(conn, monkeypatch)
    result = rebuild_links(conn)

    assert result.n_works == 1
    assert result.n_aliased == 1
    assert representative_uid(conn, "tv:tmdb:42509") == "anime:anilist:9253"
    assert query_items(conn)[0] == 1


def test_the_twin_never_represents_the_work_however_rich_it_is(conn, monkeypatch):
    """A mapped row joins a work on the map's word, not its own. Electing it
    would move *Steins;Gate* out of the anime domain because TMDB lists more
    keywords than AniList lists tags."""
    upsert_items(conn, [
        anime("anilist", 9253, 9253, description="A lab.", tags=["time travel"]),
        tmdb("tv", 42509, description="A much longer synopsis than AniList's.",
             tags=["anime", "time travel", "sci-fi", "thriller", "drama", "lab"]),
    ])
    load_map(conn, monkeypatch)
    rebuild_links(conn)
    rebuild_popularity(conn)
    rebuild_works(conn)

    by_domain = {d["domain"]: d["n_items"] for d in domain_stats(conn)}
    assert by_domain == {"anime": 1, "tv": 0}
    assert count_items(conn) == 1


def test_a_film_is_matched_through_the_movie_namespace(conn, monkeypatch):
    upsert_items(conn, [
        anime("anilist", 199, 199, title="Spirited Away", description="A bathhouse."),
        tmdb("movie", 129, title="Spirited Away"),
    ])
    load_map(conn, monkeypatch)
    assert rebuild_links(conn).n_works == 1


def test_tmdb_tv_and_movie_ids_are_different_namespaces(conn, monkeypatch):
    """TMDB film 42509 and TMDB show 42509 are unrelated; the map names a show."""
    upsert_items(conn, [anime("anilist", 9253, 9253), tmdb("movie", 42509, title="Other")])
    load_map(conn, monkeypatch)
    assert rebuild_links(conn).n_works == 0


def test_a_twin_the_map_assigns_to_several_works_is_left_alone(conn, monkeypatch):
    """TMDB keeps one show for what MAL splits into seasons. Joining the TMDB
    row to one season would be a guess, and joining it to all of them would
    merge the seasons into one work. Under-linking is the safe direction."""
    upsert_items(conn, [
        anime("anilist", 16498, 16498, title="Attack on Titan"),
        anime("anilist", 25777, 25777, title="Attack on Titan Season 2"),
        tmdb("tv", 1429, title="Attack on Titan"),
    ])
    load_map(conn, monkeypatch)
    result = rebuild_links(conn)
    assert result.n_works == 0
    assert result.n_ambiguous == 1


def test_claims_alone_never_make_a_work(conn, monkeypatch):
    """The map ties a TMDB show and a TMDB film to MAL 777, which the catalog
    does not carry. Two rows naming one absent work are not a work."""
    upsert_items(conn, [tmdb("tv", 500, title="A"), tmdb("movie", 500, title="B")])
    load_map(conn, monkeypatch)
    assert rebuild_links(conn).n_works == 0
    assert query_items(conn)[0] == 2


def test_a_twin_lends_its_poster_but_not_its_audience(conn, monkeypatch):
    """Year and image describe the work; a TMDB vote average and count describe
    TMDB's audience, which is not the anime's."""
    from recsys.db import get_item

    rep = anime("anilist", 9253, 9253, description="A lab.")
    twin = tmdb("tv", 42509).model_copy(update={
        "year": 2011, "image_url": "https://image.tmdb.org/p.jpg",
        "mean_rating": 8.4, "n_ratings": 1200,
    })
    upsert_items(conn, [rep, twin])
    load_map(conn, monkeypatch)
    rebuild_links(conn)
    item = get_item(conn, "anime:anilist:9253")
    assert (item.year, item.image_url) == (2011, "https://image.tmdb.org/p.jpg")
    assert (item.mean_rating, item.n_ratings) == (None, None)


def test_without_the_map_the_twins_stay_apart(conn):
    """Exact ids only: an equal title is not evidence."""
    upsert_items(conn, [anime("anilist", 9253, 9253), tmdb("tv", 42509)])
    assert rebuild_links(conn).n_works == 0


def test_a_twins_vote_count_never_becomes_an_animes_popularity(conn, monkeypatch):
    """Popularity is calibrated within a domain. Sharing the best figure across
    a work's rows must not reach across domains, where the numbers count
    different things on different scales."""
    upsert_items(conn, [
        anime("anilist", 9253, 9253, description="A lab.", popularity=120.0),
        tmdb("tv", 42509, popularity=5000.0),
    ])
    load_map(conn, monkeypatch)
    rebuild_links(conn)
    rebuild_popularity(conn)
    value = conn.execute(
        "SELECT value FROM popularity_norm WHERE item_uid = 'anime:anilist:9253'"
    ).fetchone()[0]
    assert value == pytest.approx(120.0)


# ---- what a user sees -----------------------------------------------------


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


@pytest.fixture
def client(monkeypatch):
    """A catalog linked through the map, read by a matrix built *before* it was.

    The matrix still holds the twin's vector -- the realistic state the day the
    map arrives, since rebuilding the matrix costs hours. Reads have to cope.
    """
    _load_similarity_index.cache_clear()
    _load_collaborative_index.cache_clear()
    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings.db_path)
    c = connect(settings.db_path)
    upsert_items(c, [
        anime("anilist", 9253, 9253, description="A lab of self-styled scientists."),
        tmdb("tv", 42509, description="Okabe discovers time travel."),
        anime("anilist", 1, 1, title="Cowboy Bebop", description="Bounty hunters."),
        tmdb("tv", 7, title="Breaking Bad", description="A chemistry teacher."),
    ])
    load_map(c, monkeypatch)
    rebuild_links(c)
    rebuild_popularity(c)
    rebuild_works(c)
    c.close()
    write_matrix(get_settings().embeddings_path, {
        "anime:anilist:9253": [1.0, 0.0],
        "tv:tmdb:42509": [0.99, 0.1],     # the twin, nearest of all
        "anime:anilist:1": [0.7, 0.7],
        "tv:tmdb:7": [0.6, 0.8],
    })
    from recsys.api.server import create_app

    with TestClient(create_app()) as tc:
        yield tc
    _load_similarity_index.cache_clear()


def test_rating_an_anime_never_recommends_its_own_tv_twin(client):
    assert client.put("/api/ratings/anime:anilist:9253", json={"rating": 9}).status_code == 200
    uids = [n["item"]["item_uid"] for n in client.get("/api/recommend").json()["neighbours"]]
    assert "tv:tmdb:42509" not in uids
    assert uids  # and the other works still come back


def test_more_like_this_never_offers_the_works_own_twin(client):
    body = client.get("/api/similar/anime:anilist:9253").json()
    uids = [n["item"]["item_uid"] for n in body["neighbours"]]
    assert "tv:tmdb:42509" not in uids
    assert "anime:anilist:9253" not in uids


def test_similar_answers_for_a_non_representative_row_too(client):
    """Opening the twin's own uid reaches the work; the matrix holds winners."""
    r = client.get("/api/similar/tv:tmdb:42509")
    assert r.status_code == 200
    uids = [n["item"]["item_uid"] for n in r.json()["neighbours"]]
    assert "anime:anilist:9253" not in uids


def test_asking_for_tv_from_an_anime_skips_its_own_twin(client):
    """The twin is the nearest TV show to its anime by construction."""
    body = client.get("/api/similar/anime:anilist:9253?domain=tv").json()
    assert [n["item"]["item_uid"] for n in body["neighbours"]] == ["tv:tmdb:7"]


def test_a_page_full_of_stale_twins_still_fills(monkeypatch):
    """Many anime, each with its TMDB twin still in an older matrix, and the
    twins nearest of all. Filtering by the matrix's stored domain and then
    translating would answer a TV request with the anime's own copies -- and
    then with nothing, once they collapse."""
    _load_similarity_index.cache_clear()
    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings.db_path)
    c = connect(settings.db_path)
    rows, claims, vectors = [], [], {}
    for n in range(1, 41):
        rows.append(anime("anilist", n, n, title=f"Anime {n}", description="x"))
        rows.append(tmdb("tv", 1000 + n, title=f"Anime {n}"))
        claims.append(("anime", "mal_id", str(n), "tv", "tmdb_id", str(1000 + n)))
        vectors[f"anime:anilist:{n}"] = [1.0, n / 1000]
        vectors[f"tv:tmdb:{1000 + n}"] = [1.0, n / 1000 + 0.0001]
    for n in range(1, 31):
        rows.append(tmdb("tv", 5000 + n, title=f"Real show {n}"))
        vectors[f"tv:tmdb:{5000 + n}"] = [0.5, 0.5 + n / 100]
    upsert_items(c, rows)
    from recsys.db import replace_id_map

    replace_id_map(c, "wikidata", claims)
    rebuild_links(c)
    rebuild_popularity(c)
    rebuild_works(c)
    c.close()
    write_matrix(settings.embeddings_path, vectors)
    from recsys.api.server import create_app

    with TestClient(create_app()) as tc:
        for n in range(1, 21):
            tc.put(f"/api/ratings/anime:anilist:{n}", json={"rating": 9})
        assert len(tc.get("/api/recommend?k=20").json()["neighbours"]) == 20
        tv = tc.get("/api/recommend?k=20&domain=tv").json()["neighbours"]
        assert len(tv) == 20
        assert all(n["item"]["title"].startswith("Real show") for n in tv)
        similar = tc.get("/api/similar/anime:anilist:1?domain=tv&k=10").json()["neighbours"]
        assert len(similar) == 10
    _load_similarity_index.cache_clear()


def test_a_work_answers_from_any_row_the_matrix_holds(client):
    """A relink can move the representative without the matrix knowing; the
    work is still answerable through the row the matrix does hold."""
    write_matrix(get_settings().embeddings_path, {
        "tv:tmdb:42509": [1.0, 0.0],      # only the twin's row was embedded
        "anime:anilist:1": [0.7, 0.7],
        "tv:tmdb:7": [0.6, 0.8],
    })
    _load_similarity_index.cache_clear()
    r = client.get("/api/similar/anime:anilist:9253")
    assert r.status_code == 200
    assert [n["item"]["item_uid"] for n in r.json()["neighbours"]] == [
        "anime:anilist:1", "tv:tmdb:7",
    ]
