"""AniList media record -> UnifiedItem, and the ingester around it.

Driven by `tests/fixtures/anilist_media_sample.json`: three real records pulled
from the live API (Cowboy Bebop, Fullmetal Alchemist: Brotherhood,
Steins;Gate), chosen because between them they cover a synopsis with markup,
spoiler-flagged tags, low-ranked tags, adaptation edges and a record with no
main studio.

No test here touches the network -- the ingester's one HTTP method is stubbed.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from recsys.config import Settings, get_settings
from recsys.db import connect, init_db
from recsys.ingest.anilist import clean_description, map_anilist_media
from recsys.ingest.anime_anilist import AniListAnimeIngester
from recsys.schemas import UnifiedItem, utcnow

FIXTURE = Path(__file__).parent / "fixtures" / "anilist_media_sample.json"


@pytest.fixture
def records() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def bebop(records) -> dict:
    return copy.deepcopy(next(r for r in records if r["idMal"] == 1))


@pytest.fixture
def steins(records) -> dict:
    return copy.deepcopy(next(r for r in records if r["idMal"] == 9253))


# ---- mapping --------------------------------------------------------------


def test_every_fixture_record_maps(records):
    items = [map_anilist_media(r, "anime") for r in records]
    assert all(isinstance(i, UnifiedItem) for i in items)
    assert len(items) == 3


def test_uid_is_the_anilist_id_not_the_mal_id(bebop):
    """A source's rows are keyed by *its own* id; mal_id is a cross-reference."""
    item = map_anilist_media(bebop, "anime")
    assert item.item_uid == "anime:anilist:1"
    assert item.external_ids["mal_id"] == "1"
    assert item.external_ids["anilist_id"] == "1"


def test_mal_id_is_present_on_every_record(records):
    """Without it nothing links, and the whole point of this source is lost."""
    for r in records:
        assert map_anilist_media(r, "anime").external_ids["mal_id"] == str(r["idMal"])


def test_score_is_rescaled_from_0_100_to_0_10(bebop):
    assert bebop["averageScore"] == 86
    assert map_anilist_media(bebop, "anime").mean_rating == 8.6


def test_missing_score_is_none(bebop):
    bebop["averageScore"] = None
    assert map_anilist_media(bebop, "anime").mean_rating is None


def test_popularity_and_n_ratings_come_from_different_fields(bebop):
    """popularity = users listing it; n_ratings = users who actually scored it."""
    item = map_anilist_media(bebop, "anime")
    assert item.popularity == float(bebop["popularity"])
    assert item.n_ratings == sum(d["amount"] for d in bebop["stats"]["scoreDistribution"])
    assert item.n_ratings < item.popularity


def test_description_markup_is_stripped(bebop):
    desc = map_anilist_media(bebop, "anime").description
    assert desc and "<br>" not in desc and "<i>" not in desc


def test_description_entities_are_unescaped():
    assert clean_description("Tom &amp; Jerry &quot;fun&quot;") == 'Tom & Jerry "fun"'


def test_description_source_note_is_dropped():
    assert clean_description("A story about things. (Source: Anime News)") == (
        "A story about things."
    )


@pytest.mark.parametrize("value", [None, "", "   ", 42, "<br><br>"])
def test_unusable_descriptions_become_none(value):
    assert clean_description(value) is None


def test_spoiler_tags_are_excluded(steins):
    """AniList flags plot-revealing tags, and item tags are shown in the UI."""
    spoilers = {t["name"].lower() for t in steins["tags"] if t["isMediaSpoiler"]}
    assert "alternate universe" in spoilers  # guards the fixture's usefulness
    assert not spoilers & set(map_anilist_media(steins, "anime").tags)


def test_spoiler_tags_survive_in_raw_json(steins):
    """Dropped from display, kept for Phase 3 -- they are strong features."""
    item = map_anilist_media(steins, "anime")
    kept = {t["name"] for t in item.raw_json["tags"] if t["isMediaSpoiler"]}
    assert "Alternate Universe" in kept


def test_low_ranked_tags_are_dropped(steins):
    low = {
        t["name"].lower()
        for t in steins["tags"]
        if not t["isMediaSpoiler"] and t["rank"] < 60
    }
    assert low and not low & set(map_anilist_media(steins, "anime").tags)


def test_min_tag_rank_is_tunable(steins):
    wide = map_anilist_media(steins, "anime", min_tag_rank=0)
    narrow = map_anilist_media(steins, "anime", min_tag_rank=90)
    assert len(wide.tags) > len(narrow.tags)


def test_the_tag_vocabulary_is_richer_than_mal_genres(steins):
    """The reason this source exists: attributes to match across domains."""
    tags = set(map_anilist_media(steins, "anime").tags)
    assert {"time manipulation", "time loop"} <= tags
    assert len(tags) > len(steins["genres"]) + 1


def test_genres_and_format_become_tags(bebop):
    tags = set(map_anilist_media(bebop, "anime").tags)
    assert {"action", "adventure", "sci-fi"} <= tags
    assert "tv" in tags


def test_main_studio_is_distinguished_from_producers(bebop):
    creators = map_anilist_media(bebop, "anime").creators
    assert ("Sunrise", "studio") in [(c.name, c.role) for c in creators]
    assert ("Bandai Visual", "producer") in [(c.name, c.role) for c in creators]


def test_titles_pick_romaji_and_keep_the_rest(bebop):
    item = map_anilist_media(bebop, "anime")
    assert item.title == "Cowboy Bebop"
    assert "カウボーイビバップ" in item.alt_titles


def test_blank_romaji_falls_through_to_english(bebop):
    """A whitespace-only title is truthy and would hide a good one behind it."""
    bebop["title"]["romaji"] = "   "
    assert map_anilist_media(bebop, "anime").title == "Cowboy Bebop"


def test_record_without_any_title_is_skipped(bebop):
    bebop["title"] = {"romaji": None, "english": None, "native": None}
    assert map_anilist_media(bebop, "anime") is None


@pytest.mark.parametrize("raw", [None, {}, {"id": None}, "nonsense"])
def test_unusable_records_are_skipped(raw):
    assert map_anilist_media(raw, "anime") is None


def test_year_comes_from_season_or_start_date(bebop):
    assert map_anilist_media(bebop, "anime").year == 1998
    bebop["seasonYear"] = None
    assert map_anilist_media(bebop, "anime").year == bebop["startDate"]["year"]


def test_adaptation_edges_are_preserved_for_later(bebop):
    """Nothing consumes relations yet; keeping them costs no extra request."""
    edges = map_anilist_media(bebop, "anime").raw_json["relations"]["edges"]
    assert any(e["relationType"] in ("SOURCE", "ADAPTATION") for e in edges)


# ---- the ingester ---------------------------------------------------------


def _seed_catalog(conn, mal_ids: list[int]) -> None:
    """Insert MAL-source rows, which is what drives the AniList fetch."""
    from recsys.db import upsert_items

    upsert_items(
        conn,
        [
            UnifiedItem(
                domain="anime", source="mal", source_id=str(i), title=f"Title {i}",
                external_ids={"mal_id": str(i)}, popularity=float(1000 - i),
                fetched_at=utcnow(),
            )
            for i in mal_ids
        ],
    )


@pytest.fixture
def settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def seeded(settings, records):
    """A catalog holding the three MAL rows the fixture has AniList data for."""
    init_db()
    conn = connect()
    _seed_catalog(conn, [1, 5114, 9253])
    yield conn
    conn.close()


@pytest.fixture
def offline(monkeypatch, records):
    """Stub the one method that would hit the network."""
    async def fake_post(self, ids):
        return [copy.deepcopy(r) for r in records if r["idMal"] in ids]

    monkeypatch.setattr(AniListAnimeIngester, "_post_batch", fake_post)


def test_fetch_is_driven_by_the_mal_ids_already_in_the_catalog(seeded, settings):
    ids = AniListAnimeIngester(settings).ids_to_fetch()
    assert set(ids) == {1, 5114, 9253}


def test_fetch_order_is_most_popular_first(seeded, settings):
    """So an interrupted or --limit run enriches titles anyone would look at."""
    assert AniListAnimeIngester(settings).ids_to_fetch() == [1, 5114, 9253]


def test_its_own_rows_are_not_refetched_as_input(seeded, settings, records):
    """Only non-anilist rows seed the id list, or a re-run would compound."""
    from recsys.db import upsert_items

    upsert_items(seeded, [map_anilist_media(records[0], "anime")])
    assert set(AniListAnimeIngester(settings).ids_to_fetch()) == {1, 5114, 9253}


def test_target_count_limits_the_id_list(seeded, settings):
    ingester = AniListAnimeIngester(settings)
    ingester.target_count = 2  # what bootstrap.py --limit sets
    assert len(ingester.ids_to_fetch()) == 2


async def test_run_inserts_alongside_the_mal_rows(seeded, settings, offline):
    result = await AniListAnimeIngester(settings).run(seeded)

    assert result.status == "ok"
    assert (result.n_fetched, result.n_inserted) == (3, 3)
    assert seeded.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 6
    assert seeded.execute(
        "SELECT COUNT(*) FROM items WHERE source='anilist'"
    ).fetchone()[0] == 3


async def test_run_is_idempotent(seeded, settings, offline):
    await AniListAnimeIngester(settings).run(seeded)
    result = await AniListAnimeIngester(settings).run(seeded)
    assert (result.n_inserted, result.n_updated) == (0, 3)


async def test_run_gives_the_mal_rows_a_synopsis_to_link_to(seeded, settings, offline):
    """The whole point: text the CSV dump does not have."""
    await AniListAnimeIngester(settings).run(seeded)
    row = seeded.execute(
        "SELECT description FROM items WHERE item_uid='anime:anilist:9253'"
    ).fetchone()
    assert row["description"] and len(row["description"]) > 200


async def test_an_empty_catalog_yields_nothing_rather_than_failing(settings, offline):
    """anime_mal_csv has to run first; saying so beats crashing."""
    init_db()
    conn = connect()
    try:
        result = await AniListAnimeIngester(settings).run(conn)
        assert (result.status, result.n_fetched) == ("ok", 0)
    finally:
        conn.close()


def test_registry_exposes_the_ingester(settings):
    from recsys.ingest.registry import available, get_ingester

    assert "anime_anilist" in available()
    ingester = get_ingester("anime_anilist", settings)
    assert (ingester.domain, ingester.source) == ("anime", "anilist")
