"""Your own ratings -- the write path, at the db layer and through the API.

The catalog is what sources said; this is the one thing only you can say, so
what is pinned here is mostly about *identity*: which uid a rating lands on
when a work has several rows, and whether it survives the linker changing its
mind about which of those rows represents the work.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from recsys.config import get_settings
from recsys.db import (
    connect,
    delete_rating,
    init_db,
    query_ratings,
    rated_uids,
    set_rating,
    upsert_items,
    upsert_ratings,
)
from recsys.link import rebuild_links
from recsys.schemas import LOCAL_USER_UID, UnifiedItem, utcnow


def item(source, source_id, title, *, domain="anime", mal_id=None, tags=(), description=None):
    return UnifiedItem(
        domain=domain, source=source, source_id=str(source_id), title=title,
        tags=list(tags), description=description, fetched_at=utcnow(),
        external_ids={"mal_id": str(mal_id)} if mal_id else {},
    )


#: 5114 is described by two rows. The AniList one is richer, so the linker
#: elects it -- which is what makes "rate the other one" a real case.
SEED = [
    item("anilist", 5114, "Fullmetal Alchemist: Brotherhood", mal_id=5114,
         tags=["action", "military", "alchemy"], description="Two brothers."),
    item("mal", 5114, "Fullmetal Alchemist: Brotherhood", mal_id=5114),
    item("anilist", 9253, "Steins;Gate", tags=["sci-fi"], description="A microwave."),
    item("anilist", 1, "A Novel", domain="book", tags=["prose"], description="Words."),
]


@pytest.fixture
def conn():
    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings.db_path)
    c = connect(settings.db_path)
    upsert_items(c, SEED)
    rebuild_links(c)
    yield c
    c.close()


@pytest.fixture
def client(conn):
    from recsys.api.server import create_app

    with TestClient(create_app()) as c:
        yield c


# ---- identity ------------------------------------------------------------


def test_a_rating_lands_on_the_row_that_represents_the_work(conn):
    stored = set_rating(conn, "anime:mal:5114", 9.0)
    assert stored == "anime:anilist:5114"


def test_rating_two_rows_of_one_work_is_one_rating(conn):
    """Both uids reach the same work through `get_item`, so without resolution
    on write the primary key would happily store two opinions of one show."""
    set_rating(conn, "anime:anilist:5114", 7.0)
    set_rating(conn, "anime:mal:5114", 9.0)

    total, rows = query_ratings(conn)
    assert total == 1
    assert rows[0][0] == 9.0


def test_unrating_works_through_either_row(conn):
    set_rating(conn, "anime:anilist:5114", 9.0)
    assert delete_rating(conn, "anime:mal:5114") is True
    assert query_ratings(conn)[0] == 0


def test_unrating_something_unrated_reports_it(conn):
    assert delete_rating(conn, "anime:anilist:9253") is False


def test_rated_uids_translates_a_rating_the_linker_has_since_moved(conn):
    """The failure this guards is silent. The embedding matrix holds only
    representatives, so a rating stranded on a losing row contributes nothing
    to a profile and raises nothing -- it just quietly stops counting."""
    upsert_ratings(conn, [(LOCAL_USER_UID, "anime:mal:5114", 9.0, "2026-01-01T00:00:00+00:00")])
    assert rated_uids(conn) == {"anime:anilist:5114": 9.0}


# ---- the library ---------------------------------------------------------


def test_the_corpus_ratings_are_not_your_library(conn):
    """19.2M MAL ratings share this table; only `user_uid` separates them."""
    upsert_ratings(conn, [("mal:someone", "anime:anilist:9253", 10.0, None)])
    set_rating(conn, "anime:anilist:5114", 8.0)

    total, rows = query_ratings(conn)
    assert total == 1
    assert rows[0][2].item_uid == "anime:anilist:5114"


def test_a_rating_pointing_outside_the_catalog_does_not_appear(conn):
    """A pruned row, or an anime the ratings dump knows and this catalog never
    ingested. An inner join, so it is absent rather than a hole in the page."""
    upsert_ratings(conn, [(LOCAL_USER_UID, "anime:mal:999999", 6.0, None)])
    assert query_ratings(conn)[0] == 0


def test_the_library_filters_by_domain(conn):
    set_rating(conn, "anime:anilist:9253", 8.0)
    set_rating(conn, "book:anilist:1", 7.0)

    total, rows = query_ratings(conn, domain="book")
    assert total == 1
    assert rows[0][2].domain == "book"


def test_the_library_is_newest_first(conn):
    set_rating(conn, "anime:anilist:9253", 8.0, rated_at="2026-01-01T00:00:00+00:00")
    set_rating(conn, "book:anilist:1", 7.0, rated_at="2026-06-01T00:00:00+00:00")

    _, rows = query_ratings(conn)
    assert [r[2].item_uid for r in rows] == ["book:anilist:1", "anime:anilist:9253"]


def test_a_library_row_is_completed_from_its_siblings(conn):
    """`fill_from_siblings` runs here for the same reason it runs on listings:
    the row that represents a work is not the row that has every field."""
    upsert_items(conn, [item("mal", 5114, "FMA:B", mal_id=5114)])
    conn.execute("UPDATE items SET year = 2009 WHERE item_uid = 'anime:mal:5114'")
    conn.commit()
    set_rating(conn, "anime:anilist:5114", 9.0)

    _, rows = query_ratings(conn)
    assert rows[0][2].item_uid == "anime:anilist:5114"
    assert rows[0][2].year == 2009


# ---- the API -------------------------------------------------------------


def test_put_then_get_round_trips(client):
    body = client.put("/api/ratings/anime:anilist:9253", json={"rating": 8}).json()
    assert body["rating"] == 8.0
    assert body["item"]["title"] == "Steins;Gate"

    page = client.get("/api/library").json()
    assert page["total"] == 1
    assert page["items"][0]["item"]["item_uid"] == "anime:anilist:9253"


def test_rating_is_idempotent_over_the_api(client):
    client.put("/api/ratings/anime:anilist:9253", json={"rating": 4})
    client.put("/api/ratings/anime:anilist:9253", json={"rating": 9})

    page = client.get("/api/library").json()
    assert page["total"] == 1
    assert page["items"][0]["rating"] == 9.0


def test_rating_something_that_is_not_in_the_catalog_is_404(client):
    assert client.put("/api/ratings/anime:anilist:404", json={"rating": 5}).status_code == 404


@pytest.mark.parametrize("score", [0, 0.9, 10.1, 11, -3])
def test_the_scale_is_one_to_ten(client, score):
    r = client.put("/api/ratings/anime:anilist:9253", json={"rating": score})
    assert r.status_code == 422


def test_delete_removes_it(client):
    client.put("/api/ratings/anime:anilist:9253", json={"rating": 8})
    assert client.delete("/api/ratings/anime:anilist:9253").status_code == 204
    assert client.get("/api/library").json()["total"] == 0


def test_deleting_an_unrated_item_is_404(client):
    assert client.delete("/api/ratings/anime:anilist:9253").status_code == 404


def test_the_library_rejects_an_unknown_domain(client):
    assert client.get("/api/library?domain=poetry").status_code == 422


def test_a_rating_is_readable_back(client):
    client.put("/api/ratings/anime:anilist:9253", json={"rating": 8})
    body = client.get("/api/ratings/anime:anilist:9253").json()
    assert body["rating"] == 8.0
    assert body["item"]["title"] == "Steins;Gate"


def test_a_rating_is_readable_through_any_row_of_the_work(client):
    """Opening the MAL row of a work you rated on the AniList row has to show
    the score, or the UI offers to rate it a second time."""
    client.put("/api/ratings/anime:anilist:5114", json={"rating": 9})
    assert client.get("/api/ratings/anime:mal:5114").json()["rating"] == 9.0


def test_reading_a_rating_you_never_made_is_404(client):
    assert client.get("/api/ratings/anime:anilist:9253").status_code == 404
