"""TMDB detail records -> UnifiedItem, for the movie and tv domains.

Driven by real captured responses: *Inception* and *The Lord of the Rings: The
Return of the King*, *Breaking Bad* and *Arcane*. Between them they cover the
four field names TMDB spells differently for film and television, and the two
places an IMDb id can hide.

The credential tests are not ceremony. A key in a URL ends up inside every
cached response envelope on disk, and this catalog caches tens of thousands of
them.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from recsys.config import Settings, get_settings
from recsys.db import connect, init_db
from recsys.ingest.movie_tmdb import TmdbMovieIngester
from recsys.ingest.tmdb import MAX_CAST, map_tmdb
from recsys.ingest.tv_tmdb import TmdbTvIngester
from recsys.schemas import UnifiedItem

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def movies() -> list[dict]:
    return _load("tmdb_movie_sample.json")


@pytest.fixture
def shows() -> list[dict]:
    return _load("tmdb_tv_sample.json")


@pytest.fixture
def inception(movies) -> dict:
    return copy.deepcopy(next(m for m in movies if m["id"] == 27205))


@pytest.fixture
def breaking_bad(shows) -> dict:
    return copy.deepcopy(next(s for s in shows if s["id"] == 1396))


@pytest.fixture
def settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


def as_movie(raw: dict) -> UnifiedItem | None:
    return map_tmdb(
        raw, "movie", title_field="title", original_title_field="original_title",
        date_field="release_date", keyword_key="keywords",
        image_base="https://image.tmdb.org/t/p/w500",
    )


def as_tv(raw: dict) -> UnifiedItem | None:
    return map_tmdb(
        raw, "tv", title_field="name", original_title_field="original_name",
        date_field="first_air_date", keyword_key="results",
        image_base="https://image.tmdb.org/t/p/w500",
    )


# ---- both domains map -----------------------------------------------------


def test_every_fixture_record_maps(movies, shows):
    assert all(as_movie(m) is not None for m in movies)
    assert all(as_tv(s) is not None for s in shows)


def test_movies_and_shows_land_in_their_own_domains(inception, breaking_bad):
    assert as_movie(inception).item_uid == "movie:tmdb:27205"
    assert as_tv(breaking_bad).item_uid == "tv:tmdb:1396"


def test_the_two_media_use_different_title_fields(inception, breaking_bad):
    assert as_movie(inception).title == "Inception"
    assert as_tv(breaking_bad).title == "Breaking Bad"


def test_the_two_media_use_different_date_fields(inception, breaking_bad):
    assert as_movie(inception).year == 2010
    assert as_tv(breaking_bad).year == 2008


# ---- the reason TMDB was chosen -------------------------------------------


def test_keywords_reach_the_tags(inception):
    """Genres are 19 buckets; keywords are what make a film comparable to an
    anime on anything finer than "both are Action"."""
    tags = set(as_movie(inception).tags)
    assert {"dreams", "heist"} <= tags
    assert "science fiction" in tags  # a genre, lowercased by the schema


def test_the_keyword_container_differs_between_media(breaking_bad):
    """Film puts them under keywords.keywords, television under
    keywords.results. Reading the wrong one loses them silently."""
    assert breaking_bad["keywords"]["results"]
    tags = set(as_tv(breaking_bad).tags)
    assert "drug dealer" in tags


def test_tags_are_far_richer_than_genres_alone(inception):
    item = as_movie(inception)
    assert len(item.tags) > len(inception["genres"]) + 3


def test_overview_becomes_the_description(inception):
    assert len(as_movie(inception).description or "") > 200


# ---- ratings and popularity -----------------------------------------------


def test_vote_average_is_not_rescaled(inception):
    """TMDB already scores 0-10, which is the unified scale."""
    assert as_movie(inception).mean_rating == pytest.approx(inception["vote_average"])


def test_popularity_is_the_vote_count_not_tmdbs_popularity(inception):
    """TMDB's own `popularity` is a trending score recomputed daily. Storing it
    would make the catalog reshuffle on every re-ingest and would not measure
    audience size at all."""
    item = as_movie(inception)
    assert item.popularity == float(inception["vote_count"])
    assert item.popularity != pytest.approx(inception["popularity"])
    assert item.n_ratings == inception["vote_count"]


def test_a_scoreless_record_has_no_rating(inception):
    inception["vote_average"] = 0
    inception["vote_count"] = 0
    item = as_movie(inception)
    assert (item.popularity, item.n_ratings) == (None, None)


# ---- identifiers ----------------------------------------------------------


def test_the_imdb_id_is_carried_for_films(inception):
    """MovieLens ships links.csv mapping its ids to IMDb and TMDB, so a ratings
    source for this domain can join exactly rather than by title."""
    assert as_movie(inception).external_ids["imdb_id"].startswith("tt")


def test_the_imdb_id_is_found_behind_external_ids_for_television(breaking_bad):
    """Film exposes it at the top level; television only under external_ids."""
    assert "imdb_id" not in breaking_bad
    assert as_tv(breaking_bad).external_ids["imdb_id"].startswith("tt")


def test_the_tmdb_url_points_at_the_right_medium(inception, breaking_bad):
    assert "/movie/" in as_movie(inception).external_ids["tmdb_url"]
    assert "/tv/" in as_tv(breaking_bad).external_ids["tmdb_url"]


def test_the_image_url_is_absolute(inception):
    assert as_movie(inception).image_url.startswith("https://image.tmdb.org/")


# ---- creators -------------------------------------------------------------


def test_the_director_leads_a_film(inception):
    creators = as_movie(inception).creators
    assert (creators[0].name, creators[0].role) == ("Christopher Nolan", "director")


def test_the_creator_leads_a_show(breaking_bad):
    creators = as_tv(breaking_bad).creators
    assert (creators[0].name, creators[0].role) == ("Vince Gilligan", "creator")


def test_cast_is_capped(inception):
    cast = [c for c in as_movie(inception).creators if c.role == "cast"]
    assert len(cast) == MAX_CAST


def test_someone_who_directs_and_acts_is_not_listed_twice(inception):
    inception["credits"]["cast"] = [
        {"name": "Christopher Nolan"},
        *inception["credits"]["cast"],
    ]
    names = [c.name for c in as_movie(inception).creators]
    assert names.count("Christopher Nolan") == 1


def test_a_record_with_no_credits_still_maps(inception):
    inception["credits"] = {}
    assert as_movie(inception).creators == []


# ---- unusable input -------------------------------------------------------


@pytest.mark.parametrize("raw", [None, {}, {"id": None}, "nonsense"])
def test_unusable_records_are_skipped(raw):
    assert as_movie(raw) is None


def test_a_record_without_a_title_is_skipped(inception):
    inception["title"] = inception["original_title"] = None
    assert as_movie(inception) is None


def test_a_missing_release_date_is_not_a_year(inception):
    inception["release_date"] = ""
    assert as_movie(inception).year is None


# ---- filters: what does not earn a place in the catalog -------------------


def test_adult_records_are_rejected(inception):
    inception["adult"] = True
    assert as_movie(inception) is None


def test_direct_to_video_records_are_rejected(inception):
    """`video: true` marks a record that is not a release."""
    inception["video"] = True
    assert as_movie(inception) is None


def test_a_title_below_the_vote_floor_is_rejected(inception):
    """A score built on four votes is not a score. Measured on an unfiltered
    pass: 22% of films had fewer than 50 votes and 53% of shows fewer than 25."""
    inception["vote_count"] = 4
    assert map_tmdb(
        inception, "movie", title_field="title", original_title_field="original_title",
        date_field="release_date", keyword_key="keywords",
        image_base="https://image.tmdb.org/t/p/w500", min_votes=50,
    ) is None


def test_a_well_voted_title_survives_the_floor(inception):
    assert map_tmdb(
        inception, "movie", title_field="title", original_title_field="original_title",
        date_field="release_date", keyword_key="keywords",
        image_base="https://image.tmdb.org/t/p/w500", min_votes=50,
    ) is not None


def test_television_takes_a_lower_floor_than_film(settings):
    """A well-known show gathers fewer votes than a mid-tier film, so the film
    threshold would halve the domain for the wrong reason."""
    assert TmdbTvIngester(settings).min_votes < TmdbMovieIngester(settings).min_votes


def test_the_vote_floor_is_sent_to_discover(settings):
    """Filtered at the source: a rejected row is a detail request never made."""
    ingester = TmdbMovieIngester(settings)
    assert ingester.min_votes > 0


# ---- credentials never reach disk -----------------------------------------


def test_the_cache_key_excludes_the_api_key(settings):
    """DiskCache writes the string it is keyed on into every envelope, so a key
    left here would be stored in plaintext once per cached response."""
    ingester = TmdbMovieIngester(settings)
    key = ingester._cache_key("/movie/27205", {"api_key": "SECRETVALUE", "page": 3})
    assert "SECRETVALUE" not in key
    assert "page=3" in key


def test_the_cache_key_still_separates_different_requests(settings):
    ingester = TmdbMovieIngester(settings)
    a = ingester._cache_key("/movie/1", {"append_to_response": "credits"})
    b = ingester._cache_key("/movie/2", {"append_to_response": "credits"})
    assert a != b


def test_a_bearer_token_is_sent_as_a_header_not_a_query_param(monkeypatch):
    """The whole reason to prefer the v4 token: the secret never reaches a URL,
    a log line or a cache file."""
    monkeypatch.setenv("TMDB_READ_TOKEN", "tok-abc")
    monkeypatch.setenv("TMDB_API_KEY", "")
    get_settings.cache_clear()
    ingester = TmdbMovieIngester(get_settings())
    try:
        assert ingester._get_client().headers["Authorization"] == "Bearer tok-abc"
    finally:
        ingester._client = None


def test_without_a_token_no_authorization_header_is_set(monkeypatch):
    monkeypatch.setenv("TMDB_READ_TOKEN", "")
    monkeypatch.setenv("TMDB_API_KEY", "key-abc")
    get_settings.cache_clear()
    ingester = TmdbMovieIngester(get_settings())
    try:
        assert "Authorization" not in ingester._get_client().headers
    finally:
        ingester._client = None


async def test_no_credential_at_all_fails_loudly(monkeypatch, tmp_path):
    """Silently fetching nothing would look like an empty upstream."""
    monkeypatch.setenv("TMDB_READ_TOKEN", "")
    monkeypatch.setenv("TMDB_API_KEY", "")
    get_settings.cache_clear()

    path = tmp_path / "tmdb.db"
    init_db(path)
    conn = connect(path)
    try:
        result = await TmdbMovieIngester(get_settings()).run(conn)
        assert result.status == "error"
        assert "TMDB_READ_TOKEN" in result.error
    finally:
        conn.close()


# ---- the invariant --------------------------------------------------------


def test_registry_exposes_both_ingesters(settings):
    from recsys.ingest.registry import available, get_ingester

    assert {"movie_tmdb", "tv_tmdb"} <= set(available())
    assert get_ingester("movie_tmdb", settings).domain == "movie"
    assert get_ingester("tv_tmdb", settings).domain == "tv"


def test_the_two_domains_share_one_client(settings):
    """Adding tv after movie was four field names, not a second HTTP stack."""
    assert TmdbTvIngester(settings).base_url == TmdbMovieIngester(settings).base_url
    assert TmdbTvIngester.append != TmdbMovieIngester.append
