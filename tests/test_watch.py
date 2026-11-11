"""Where to watch: a link that always works, and the services when TMDB can say.

Nothing stored says where a work streams, and it changes too often to store.
TMDB keeps a watch page for every film and show, per region, so a link to it
needs no credential. With one, TMDB's watch-providers endpoint (JustWatch's
data) names the services. The lookup is a parameter, so no test touches the
network.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from recsys.api import routes
from recsys.config import get_settings
from recsys.db import connect, init_db, upsert_items
from recsys.schemas import UnifiedItem, utcnow
from recsys.watch import where_to_watch

PROVIDERS = {
    "id": 603,
    "results": {
        "BR": {
            "link": "https://www.themoviedb.org/movie/603-the-matrix/watch?locale=BR",
            "flatrate": [{"provider_name": "Max", "logo_path": "/max.jpg", "display_priority": 1}],
            "rent": [{"provider_name": "Apple TV", "logo_path": "/apple.jpg"}],
            "buy": [{"provider_name": "Apple TV", "logo_path": "/apple.jpg"},
                    {"provider_name": "Google Play", "logo_path": "/gp.jpg"}],
        },
        "US": {"flatrate": [{"provider_name": "Netflix", "logo_path": "/n.jpg"}]},
    },
}


def row(domain, source, source_id, title, **ids):
    return UnifiedItem(domain=domain, source=source, source_id=source_id, title=title,
                       external_ids=ids, fetched_at=utcnow())


@pytest.fixture
def conn():
    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings.db_path)
    c = connect(settings.db_path)
    upsert_items(c, [
        row("movie", "tmdb", "603", "The Matrix", tmdb_id="603"),
        row("anime", "anilist", "1", "Cowboy Bebop", anilist_url="https://anilist.co/anime/1",
            mal_url="https://myanimelist.net/anime/1"),
        row("tv", "tmdb", "30991", "Cowboy Bebop", tmdb_id="30991"),
        row("anime", "mal", "9", "Only On MAL", mal_url="https://myanimelist.net/anime/9"),
        row("book", "goodbooks", "1", "A Book"),
    ])
    # The anime and its own TMDB entry are one work (the Wikidata id map's job).
    c.executemany("INSERT INTO links VALUES (?, ?, ?)", [
        ("anime:anilist:1", "w1", 1), ("tv:tmdb:30991", "w1", 0),
    ])
    c.commit()
    yield c
    c.close()


def fake(data):
    calls = []

    def fetch(media, tmdb_id):
        calls.append((media, tmdb_id))
        return data

    fetch.calls = calls
    return fetch


def test_a_film_links_to_its_watch_page_in_your_region(conn):
    info = where_to_watch(conn, "movie:tmdb:603", region="BR", fetch=None)
    assert info.link == "https://www.themoviedb.org/movie/603/watch?locale=BR"
    assert info.providers is None  # not looked up: no credential


def test_the_services_come_from_your_region_only(conn):
    info = where_to_watch(conn, "movie:tmdb:603", region="BR", fetch=fake(PROVIDERS))
    assert [(p.name, p.kind) for p in info.providers] == [
        ("Max", "stream"), ("Apple TV", "rent"), ("Google Play", "buy"),
    ]
    assert info.providers[0].logo_url == "https://image.tmdb.org/t/p/w92/max.jpg"
    assert info.link == PROVIDERS["results"]["BR"]["link"]
    assert info.credit == "JustWatch"


def test_a_service_offered_two_ways_is_listed_once_at_its_cheapest(conn):
    """Apple TV rents and sells; renting is what a viewer wants to know."""
    info = where_to_watch(conn, "movie:tmdb:603", region="BR", fetch=fake(PROVIDERS))
    assert [p.name for p in info.providers].count("Apple TV") == 1


def test_nowhere_in_your_region_is_an_empty_list_not_unknown(conn):
    info = where_to_watch(conn, "movie:tmdb:603", region="JP", fetch=fake(PROVIDERS))
    assert info.providers == []


def test_an_anime_is_looked_up_through_its_own_tmdb_entry(conn):
    fetch = fake(PROVIDERS)
    info = where_to_watch(conn, "anime:anilist:1", region="BR", fetch=fetch)
    assert fetch.calls == [("tv", 30991)]
    assert info.providers


def test_an_anime_tmdb_does_not_know_links_to_its_own_pages(conn):
    """AniList's page lists a show's legal streams; MAL's when that is all."""
    info = where_to_watch(conn, "anime:mal:9", region="BR", fetch=fake(PROVIDERS))
    assert info.link == "https://myanimelist.net/anime/9"
    assert info.providers is None


def test_a_failed_lookup_still_gives_the_link(conn):
    def broken(media, tmdb_id):
        raise OSError("network down")

    info = where_to_watch(conn, "movie:tmdb:603", region="BR", fetch=broken)
    assert info.link.endswith("/movie/603/watch?locale=BR")
    assert info.providers is None


def test_a_book_has_nowhere_to_watch(conn):
    info = where_to_watch(conn, "book:goodbooks:1", region="BR", fetch=fake(PROVIDERS))
    assert info.link is None and info.providers is None


def test_the_route(conn, monkeypatch):
    monkeypatch.setattr(routes, "_watch_fetcher", lambda cfg: fake(PROVIDERS))
    from recsys.api.server import create_app

    with TestClient(create_app()) as client:
        body = client.get("/api/watch/movie:tmdb:603").json()
        assert body["region"] == "BR"
        assert body["providers"][0]["name"] == "Max"
        assert client.get("/api/watch/movie:tmdb:404").status_code == 404
