"""MusicBrainz artists -> UnifiedItem, and the guards that keep the domain useful.

This domain exists on a narrower justification than the others, and the tests
pin that justification rather than just the mapping. A general music catalog was
measured and rejected: music tags describe sound while every other domain's tags
describe story, so the vocabulary overlap is nationality, decade and occupation
-- coincidence, not similarity. What survives is the soundtrack link, so the
ingester is driven by composers the catalog already credits.

The name-matching guard is the important one. "John Williams" is several people,
and a wrong match would attach an unrelated artist's genres to a film composer
and teach the bridge something false.
"""

from __future__ import annotations

import pytest

from recsys.config import Settings, get_settings
from recsys.db import connect, init_db, upsert_items
from recsys.ingest.music_musicbrainz import (
    MusicBrainzIngester,
    _is_useful_tag,
    map_artist,
    normalise_name,
)
from recsys.schemas import UnifiedItem, utcnow


def artist(**over) -> dict:
    base = {
        "id": "a74b1b7f-71a5-4011-9441-d0b5e4122711",
        "name": "Radiohead",
        "type": "Group",
        "score": 100,
        "tags": [
            {"name": "rock", "count": 9},
            {"name": "alternative rock", "count": 6},
            {"name": "english", "count": 3},
            {"name": "bootleg", "count": 1},
            {"name": "2000s", "count": 1},
        ],
        "_scored_titles": 3,
        "_deezer_fans": 4081372,
        "_deezer_id": 399,
    }
    base.update(over)
    return base


def film(uid: str, crew: list[tuple[str, str]]) -> UnifiedItem:
    """A film row carrying crew credits, the way movie_tmdb leaves them."""
    return UnifiedItem(
        domain="movie", source="tmdb", source_id=uid, title=f"Film {uid}",
        fetched_at=utcnow(),
        raw_json={"credits": {"crew": [{"name": n, "job": j} for n, j in crew]}},
    )


@pytest.fixture
def settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def seeded(settings):
    init_db()
    conn = connect()
    yield conn
    conn.close()


# ---- mapping --------------------------------------------------------------


def test_an_artist_maps_into_the_music_domain():
    item = map_artist(artist())
    assert item.domain == "music"
    assert item.item_uid.startswith("music:musicbrainz:")


def test_the_artist_is_its_own_creator():
    """The point of the domain: "same person" reaches music with no special case,
    so a film's composer and that composer's own entry connect."""
    item = map_artist(artist())
    assert [(c.name, c.role) for c in item.creators] == [("Radiohead", "artist")]


def test_musicbrainz_tags_become_tags():
    tags = set(map_artist(artist()).tags)
    assert {"rock", "alternative rock"} <= tags


def test_the_artist_type_is_a_facet():
    """A Person who scores films and an Orchestra are different things."""
    assert "group" in map_artist(artist()).tags


@pytest.mark.parametrize("noise", ["bootleg", "list", "seen live", "composer", "singer", "band"])
def test_paperwork_tags_are_dropped(noise):
    assert not _is_useful_tag(noise)


@pytest.mark.parametrize("decade", ["2000s", "80s", "1990s", "70s"])
def test_decade_tags_are_dropped(decade):
    """A decade is not a genre, and decades are a large part of the spurious
    overlap with the other domains' vocabularies."""
    assert not _is_useful_tag(decade)


@pytest.mark.parametrize("good", ["rock", "post-rock", "film score", "idm", "jazz"])
def test_real_genre_tags_survive(good):
    assert _is_useful_tag(good)


def test_deezer_supplies_the_popularity():
    """MusicBrainz has no popularity signal at all."""
    item = map_artist(artist())
    assert item.popularity == 4081372.0
    assert item.external_ids["deezer_id"] == "399"


def test_a_missing_fan_count_is_not_fatal():
    item = map_artist(artist(_deezer_fans=None, _deezer_id=None))
    assert item.popularity is None
    assert "deezer_id" not in item.external_ids


def test_no_rating_is_invented():
    """Nobody rated anything here, and play counts are not opinions."""
    item = map_artist(artist())
    assert item.mean_rating is None and item.n_ratings is None


def test_musicbrainz_has_no_description():
    """A documented gap: the strongest bridge has nothing to embed for music."""
    assert map_artist(artist()).description is None


def test_the_link_back_to_the_catalog_is_preserved():
    """How many catalog titles this artist scored -- the reason they are here."""
    assert map_artist(artist()).raw_json["_scored_titles"] == 3


@pytest.mark.parametrize("bad", [{}, {"id": None, "name": "x"}, {"id": "x", "name": ""}])
def test_unusable_records_are_skipped(bad):
    assert map_artist(bad) is None


# ---- who gets fetched -----------------------------------------------------


def test_composers_are_taken_from_film_crew(seeded, settings):
    upsert_items(seeded, [
        film("1", [("Hans Zimmer", "Original Music Composer"), ("Nolan", "Director")]),
        film("2", [("Yoko Kanno", "Music")]),
    ])
    names = dict(MusicBrainzIngester(settings).composers_in_catalog())
    assert set(names) == {"Hans Zimmer", "Yoko Kanno"}


def test_directors_and_cast_are_not_composers(seeded, settings):
    upsert_items(seeded, [
        film("1", [("A Director", "Director"), ("An Editor", "Editor")]),
    ])
    assert MusicBrainzIngester(settings).composers_in_catalog() == []


def test_the_busiest_composers_come_first(seeded, settings):
    upsert_items(seeded, [
        film("1", [("Prolific", "Original Music Composer")]),
        film("2", [("Prolific", "Original Music Composer")]),
        film("3", [("Occasional", "Original Music Composer")]),
    ])
    assert [n for n, _ in MusicBrainzIngester(settings).composers_in_catalog()] == [
        "Prolific", "Occasional",
    ]


def test_the_count_of_scored_titles_is_carried(seeded, settings):
    upsert_items(seeded, [
        film(str(i), [("Prolific", "Original Music Composer")]) for i in range(4)
    ])
    assert MusicBrainzIngester(settings).composers_in_catalog() == [("Prolific", 4)]


def test_target_count_limits_the_list(seeded, settings):
    upsert_items(seeded, [
        film(str(i), [(f"Composer {i}", "Original Music Composer")]) for i in range(5)
    ])
    ingester = MusicBrainzIngester(settings)
    ingester.target_count = 2
    assert len(ingester.composers_in_catalog()) == 2


async def test_an_empty_catalog_yields_nothing_rather_than_failing(settings, seeded):
    """movie_tmdb has to run first; saying so beats crashing."""
    result = await MusicBrainzIngester(settings).run(seeded)
    assert (result.status, result.n_fetched) == ("ok", 0)


# ---- the name-matching guard ----------------------------------------------


@pytest.mark.parametrize(
    "a,b", [("Hans Zimmer", "hans zimmer"), ("John  Williams", "John Williams")]
)
def test_names_compare_after_normalisation(a, b):
    assert normalise_name(a) == normalise_name(b)


async def test_a_low_scoring_match_is_rejected(settings, seeded, monkeypatch):
    async def fake(self, url, params, *, deezer=False):
        return {"artists": [{"id": "x", "name": "Hans Zimmer", "score": 50}]}

    monkeypatch.setattr(MusicBrainzIngester, "_get_json", fake)
    upsert_items(seeded, [film("1", [("Hans Zimmer", "Original Music Composer")])])
    result = await MusicBrainzIngester(settings).run(seeded)
    assert (result.n_fetched, result.n_skipped) == (0, 1)


async def test_a_differently_named_match_is_rejected(settings, seeded, monkeypatch):
    """The guard that matters: "John Williams" is several unrelated people, and
    a confident wrong match would teach the bridge something false."""
    async def fake(self, url, params, *, deezer=False):
        return {"artists": [{"id": "x", "name": "John Williams & Friends", "score": 100}]}

    monkeypatch.setattr(MusicBrainzIngester, "_get_json", fake)
    upsert_items(seeded, [film("1", [("John Williams", "Original Music Composer")])])
    result = await MusicBrainzIngester(settings).run(seeded)
    assert (result.n_fetched, result.n_skipped) == (0, 1)


async def test_an_exact_high_scoring_match_is_accepted(settings, seeded, monkeypatch):
    async def fake(self, url, params, *, deezer=False):
        if "deezer" in url:
            return {"data": [{"name": "Hans Zimmer", "nb_fan": 12345, "id": 7}]}
        return {"artists": [{"id": "mbid-1", "name": "Hans Zimmer", "score": 100,
                             "type": "Person", "tags": [{"name": "film score", "count": 5}]}]}

    monkeypatch.setattr(MusicBrainzIngester, "_get_json", fake)
    upsert_items(seeded, [film("1", [("Hans Zimmer", "Original Music Composer")])])
    result = await MusicBrainzIngester(settings).run(seeded)

    assert (result.n_fetched, result.n_inserted) == (1, 1)
    row = seeded.execute(
        "SELECT title, popularity, tags FROM items WHERE domain='music'"
    ).fetchone()
    assert row["title"] == "Hans Zimmer"
    assert row["popularity"] == 12345.0
    assert "film score" in row["tags"]


async def test_one_unresolvable_artist_does_not_end_the_run(settings, seeded, monkeypatch):
    """A flaky upstream should cost coverage, not the whole ingest. MusicBrainz
    503s intermittently at its own stated rate limit, and a run resolving
    several thousand names will meet that."""
    calls = {"n": 0}

    async def fake(self, url, params, *, deezer=False):
        if "deezer" in url:
            return {"data": []}
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("request failed after 8 attempts (HTTP 503)")
        return {"artists": [{"id": f"mbid-{calls['n']}", "name": "Second Composer",
                             "score": 100, "tags": []}]}

    monkeypatch.setattr(MusicBrainzIngester, "_get_json", fake)
    upsert_items(seeded, [
        film("1", [("First Composer", "Original Music Composer")]),
        film("2", [("First Composer", "Original Music Composer")]),
        film("3", [("Second Composer", "Original Music Composer")]),
    ])
    result = await MusicBrainzIngester(settings).run(seeded)

    assert result.status == "ok"
    assert (result.n_fetched, result.n_skipped) == (1, 1)


def test_the_slowdown_floor_is_not_jittered_to_nothing():
    """`backoff_delay` jitters from zero, which would let every retry fire
    inside the same bad second. A floor makes the client actually wait."""
    from recsys.ingest.music_musicbrainz import SLOWDOWN_FLOOR

    assert SLOWDOWN_FLOOR >= 1.0


def test_the_sustained_rate_stays_under_the_stated_limit(settings):
    """MusicBrainz asks for at most 1/s and throttles clients that sit exactly
    at it, so the per-minute window holds the average below."""
    assert settings.musicbrainz_rate_per_min / 60.0 < settings.musicbrainz_rate_per_sec


def test_the_user_agent_names_a_contact(settings):
    """MusicBrainz throttles clients that identify no way to reach them.
    Measured: 3 of 4 requests returned 503 with a bare agent string, 4 of 4
    succeeded once a contact URL was added. This is not cosmetic."""
    ua = MusicBrainzIngester(settings).user_agent
    assert ua.startswith("Loom/")
    assert "(" in ua and ")" in ua
    contact = ua[ua.index("(") + 1 : ua.rindex(")")].strip()
    assert contact and ("://" in contact or "@" in contact)


def test_registry_exposes_the_ingester(settings):
    from recsys.ingest.registry import available, get_ingester

    assert "music_musicbrainz" in available()
    ingester = get_ingester("music_musicbrainz", settings)
    assert (ingester.domain, ingester.source) == ("music", "musicbrainz")


# ---- pictures -------------------------------------------------------------
#
# MusicBrainz has no images, so every music card said "no image" -- 6,556 of
# 6,556. Deezer, already asked for the audience count, returns the artist's
# picture in the same search answer.

DZ = "https://e-cdns-images.dzcdn.net/images/artist"


def test_deezers_picture_is_the_artists_image():
    item = map_artist(artist(_deezer_picture=f"{DZ}/7a1b2c/500x500-000000-80-0-0.jpg"))
    assert item.image_url == f"{DZ}/7a1b2c/500x500-000000-80-0-0.jpg"


@pytest.mark.parametrize("url", [
    f"{DZ}//500x500-000000-80-0-0.jpg",  # an empty hash: Deezer has no picture
    f"{DZ}/d41d8cd98f00b204e9800998ecf8427e/500x500-000000-80-0-0.jpg",  # md5("")
    "", None, "not a url",
])
def test_deezers_placeholder_is_no_image(url):
    assert map_artist(artist(_deezer_picture=url)).image_url is None


async def test_the_picture_comes_from_the_answer_already_fetched(settings, seeded, monkeypatch):
    calls = []

    async def fake(self, url, params, *, deezer=False):
        calls.append(url)
        if "deezer" in url:
            return {"data": [{"name": "Hans Zimmer", "nb_fan": 12345, "id": 7,
                              "picture_big": f"{DZ}/ab12/500x500-000000-80-0-0.jpg",
                              "picture_xl": f"{DZ}/ab12/1000x1000-000000-80-0-0.jpg"}]}
        return {"artists": [{"id": "mbid-1", "name": "Hans Zimmer", "score": 100,
                             "type": "Person", "tags": []}]}

    monkeypatch.setattr(MusicBrainzIngester, "_get_json", fake)
    upsert_items(seeded, [film("1", [("Hans Zimmer", "Original Music Composer")])])
    await MusicBrainzIngester(settings).run(seeded)

    row = seeded.execute("SELECT image_url FROM items WHERE domain='music'").fetchone()
    assert row["image_url"] == f"{DZ}/ab12/500x500-000000-80-0-0.jpg"
    assert len(calls) == 2  # one MusicBrainz search, one Deezer search; no extra request


class Recorder:
    """A limiter that remembers it was asked, and never waits."""

    def __init__(self, name, log):
        self.name, self.log = name, log

    async def acquire(self):
        self.log.append(self.name)


def transport_for(handler):
    import httpx

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_each_service_is_paced_by_its_own_limiter(settings):
    """MusicBrainz at under one a second, Deezer at its own 50 per 5 s. Swapping
    them would run MusicBrainz at eight a second, which gets a client blocked."""
    import httpx

    assert settings.deezer_rate_per_sec * 5 < 50
    ingester = MusicBrainzIngester(settings)
    acquired: list[str] = []
    ingester.limiter = Recorder("musicbrainz", acquired)
    ingester.deezer_limiter = Recorder("deezer", acquired)
    ingester._client = transport_for(lambda request: httpx.Response(200, json={"data": []}))
    await ingester._get_json(f"{ingester.url}/artist/", {"query": "x"})
    await ingester._get_json(f"{ingester.deezer_url}/search/artist", {"q": "x"}, deezer=True)
    await ingester.aclose()
    assert acquired == ["musicbrainz", "deezer"]


async def test_a_deezer_quota_error_is_retried_and_never_cached(settings, monkeypatch):
    """Deezer answers HTTP 200 with {"error": {"code": 4}} when over quota.
    Cached, that would read as "no such artist" -- no picture, no fans -- for
    the cache's whole lifetime."""
    import httpx

    import recsys.ingest.music_musicbrainz as mb

    monkeypatch.setattr(mb, "DEEZER_QUOTA_PAUSE", 0.0)
    answers = [{"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}},
               {"data": [{"name": "Hans Zimmer", "nb_fan": 7, "id": 1}]}]
    ingester = MusicBrainzIngester(settings)
    ingester.deezer_limiter = Recorder("deezer", [])
    ingester._client = transport_for(lambda request: httpx.Response(200, json=answers.pop(0)))
    url, params = f"{ingester.deezer_url}/search/artist", {"q": "Hans Zimmer", "limit": 1}
    payload = await ingester._get_json(url, params, deezer=True)
    await ingester.aclose()
    assert payload["data"][0]["nb_fan"] == 7
    assert ingester.cache.get(f"{url}?{sorted(params.items())}") == payload


async def test_another_deezer_error_is_no_answer_and_is_not_kept(settings):
    import httpx

    ingester = MusicBrainzIngester(settings)
    ingester.deezer_limiter = Recorder("deezer", [])
    ingester._client = transport_for(
        lambda request: httpx.Response(200, json={"error": {"code": 800}}))
    url, params = f"{ingester.deezer_url}/search/artist", {"q": "Nobody"}
    assert await ingester._get_json(url, params, deezer=True) is None
    await ingester.aclose()
    assert ingester.cache.get(f"{url}?{sorted(params.items())}") is None
