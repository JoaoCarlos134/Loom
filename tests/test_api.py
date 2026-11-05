"""The four API routes, against a seeded temp database.

The app is built inside each test so it picks up the per-test ``DATA_DIR`` from
the autouse fixture in conftest; ``TestClient`` as a context manager runs the
lifespan, which is what creates the schema.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from recsys import __version__
from recsys.config import get_settings
from recsys.db import connect, init_db, upsert_items
from recsys.schemas import DOMAINS, UnifiedItem


def item(source_id="1", *, title="Cowboy Bebop", domain="anime", **kw) -> UnifiedItem:
    return UnifiedItem(
        domain=domain, source="jikan", source_id=source_id, title=title, **kw
    )


SEED = [
    item("1", title="Cowboy Bebop", popularity=2_071_833.0, mean_rating=8.75,
         year=1998, description="A ragtag crew of bounty hunters."),
    item("5114", title="Fullmetal Alchemist: Brotherhood", popularity=3_710_157.0,
         mean_rating=9.11, year=2009, description="Two brothers search for a stone."),
    item("9253", title="Steins;Gate", popularity=2_832_911.0, mean_rating=9.07,
         year=2011, description="A microwave that sends texts to the past."),
    item("42", title="A Novel", domain="book", popularity=100.0),
]


@pytest.fixture
def client():
    """Seeded app. Schema is created before the app so both see one DB path."""
    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings.db_path)
    conn = connect(settings.db_path)
    upsert_items(conn, SEED)
    conn.close()

    from recsys.api.server import create_app

    with TestClient(create_app()) as c:
        yield c


# ---- /api/health ---------------------------------------------------------


def test_health_reports_ready_and_counts(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "status": "ok",
        "version": __version__,
        "db_ready": True,
        "n_items": len(SEED),
    }


# ---- /api/items ----------------------------------------------------------


def test_items_returns_everything_by_default(client):
    body = client.get("/api/items").json()
    assert body["total"] == len(SEED)
    assert len(body["items"]) == len(SEED)
    assert body["offset"] == 0


def test_items_ordered_by_popularity(client):
    """Within a domain. Across all of them there is no one scale, and the
    listing interleaves the domains instead (tests/test_browse_default.py)."""
    items = client.get("/api/items", params={"domain": "anime"}).json()["items"]
    pops = [i["popularity"] for i in items]
    assert pops == sorted(pops, reverse=True)


def test_items_domain_filter(client):
    body = client.get("/api/items", params={"domain": "anime"}).json()
    assert body["total"] == 3
    assert {i["domain"] for i in body["items"]} == {"anime"}


def test_domain_filter_is_case_and_space_insensitive(client):
    assert client.get("/api/items", params={"domain": "  ANIME  "}).json()["total"] == 3


@pytest.mark.parametrize("value", ["all", ""])
def test_domain_all_means_no_filter(client, value):
    assert client.get("/api/items", params={"domain": value}).json()["total"] == len(SEED)


def test_unknown_domain_is_422(client):
    r = client.get("/api/items", params={"domain": "podcast"})
    assert r.status_code == 422
    assert "podcast" in r.json()["detail"]


def test_search_hits_the_fts_index(client):
    body = client.get("/api/items", params={"q": "alchemist"}).json()
    assert body["total"] == 1
    assert body["items"][0]["item_uid"] == "anime:jikan:5114"


def test_search_matches_description(client):
    body = client.get("/api/items", params={"q": "microwave"}).json()
    assert body["items"][0]["title"] == "Steins;Gate"


def test_search_combines_with_domain(client):
    body = client.get("/api/items", params={"q": "novel", "domain": "book"}).json()
    assert body["total"] == 1


def test_search_with_no_matches_is_empty_not_404(client):
    body = client.get("/api/items", params={"q": "zzzznotathing"}).json()
    assert body["total"] == 0 and body["items"] == []


def test_blank_search_is_ignored(client):
    assert client.get("/api/items", params={"q": "   "}).json()["total"] == len(SEED)


def test_fts_operator_in_user_input_does_not_500(client):
    # Regression guard: unquoted AND / * would be FTS5 syntax, not a word.
    for q in ("a AND b", "*", '"', "NEAR"):
        assert client.get("/api/items", params={"q": q}).status_code == 200


def test_pagination(client):
    body = client.get("/api/items", params={"limit": 2, "offset": 0}).json()
    assert body["total"] == len(SEED) and len(body["items"]) == 2
    assert body["limit"] == 2

    page2 = client.get("/api/items", params={"limit": 2, "offset": 2}).json()
    first = {i["item_uid"] for i in body["items"]}
    assert first.isdisjoint({i["item_uid"] for i in page2["items"]})


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"limit": -1}, {"offset": -1}])
def test_out_of_range_paging_params_rejected(client, params):
    assert client.get("/api/items", params=params).status_code == 422


def test_listed_items_carry_no_raw_json(client):
    """ITEM_COLUMNS excludes raw_json, so it never leaves the DB layer."""
    assert client.get("/api/items").json()["items"][0]["raw_json"] is None


# ---- /api/items/{item_uid} ----------------------------------------------


def test_read_item_by_uid_with_colons(client):
    """item_uid contains ':' -- the route uses a :path converter for this."""
    r = client.get("/api/items/anime:jikan:5114")
    assert r.status_code == 200
    assert r.json()["title"] == "Fullmetal Alchemist: Brotherhood"


def test_read_item_round_trips_the_full_model(client):
    body = client.get("/api/items/anime:jikan:1").json()
    assert body["year"] == 1998
    assert body["mean_rating"] == 8.75
    assert body["domain"] == "anime"


def test_unknown_item_is_404(client):
    r = client.get("/api/items/anime:jikan:999999")
    assert r.status_code == 404
    assert "999999" in r.json()["detail"]


# ---- /api/stats ----------------------------------------------------------


def test_stats_totals_and_domains(client):
    body = client.get("/api/stats").json()
    assert body["total_items"] == len(SEED)
    by_domain = {d["domain"]: d for d in body["by_domain"]}
    assert by_domain["anime"]["n_items"] == 3
    assert by_domain["book"]["n_items"] == 1
    assert by_domain["anime"]["sources"] == ["jikan"]


def test_stats_reports_config(client):
    body = client.get("/api/stats").json()
    assert body["region"] == get_settings().region
    assert body["db_path"] == str(get_settings().db_path)


def test_stats_last_ingest_null_without_a_successful_run(client):
    # The seed writes items directly, never an ingest_runs row.
    assert client.get("/api/stats").json()["last_ingest_at"] is None


# ---- static UI -----------------------------------------------------------


def test_index_is_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_static_assets_are_not_cached(client):
    r = client.get("/app.js")
    assert r.status_code == 200
    assert "no-store" in r.headers["cache-control"]


def test_api_routes_win_over_the_static_mount(client):
    # The UI is mounted at "/", so a bad mount order would shadow /api/*.
    assert client.get("/api/health").json()["status"] == "ok"


# ---- the UI's domain filter ----------------------------------------------


def test_the_domain_filter_offers_every_domain():
    """The dropdown shipped without `manga` -- the largest domain in the
    catalog -- so the biggest slice of it was unreachable from the UI. Pinned
    against DOMAINS rather than a hand-written list, so adding a domain cannot
    quietly leave the filter behind again.
    """
    from recsys.api.server import UI_DIR

    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    missing = [d for d in DOMAINS if f'<option value="{d}">' not in html]
    assert not missing, f"index.html has no filter option for {missing}"


def test_an_upgrade_that_empties_the_read_view_is_refilled_at_startup():
    """Migrations recreate `works` empty. Left alone, the app would serve the
    slow fallback -- no series cards, no covers-first order -- until someone
    thought to run bootstrap."""
    from recsys.config import get_settings
    from recsys.db import connect, init_db, upsert_items
    from recsys.derive import rebuild_derived
    from recsys.schemas import UnifiedItem

    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings.db_path)
    conn = connect(settings.db_path)
    upsert_items(conn, [UnifiedItem(domain="book", source="goodbooks", source_id="1",
                                    title="Dark Matter", popularity=5.0)])
    rebuild_derived(conn)
    with conn:
        conn.execute("DELETE FROM works")  # what a migration that recreates it leaves
    conn.close()

    from recsys.api.server import create_app

    with TestClient(create_app()):
        pass
    conn = connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 1
    conn.close()
