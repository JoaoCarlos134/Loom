"""MAL CSV dump row -> UnifiedItem.

Driven by `tests/fixtures/mal_animelist_sample.csv`: four real rows lifted from
the Kaggle dump, chosen to cover the fully-populated case, an unrated row
(`score` = 0) and a row with no studio.
"""

from __future__ import annotations

import copy
import csv
from pathlib import Path

import pytest

from recsys.config import Settings, get_settings
from recsys.ingest.anime_mal_csv import (
    MalCsvAnimeIngester,
    _parse_year,
    _split_list,
    map_mal_row,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mal_animelist_sample.csv"


@pytest.fixture
def rows() -> list[dict]:
    with FIXTURE.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture
def bebop(rows) -> dict:
    return copy.deepcopy(next(r for r in rows if r["anime_id"] == "1"))


# ---- the happy path ------------------------------------------------------


def test_every_fixture_row_maps(rows):
    items = [map_mal_row(r) for r in rows]
    assert all(i is not None for i in items)
    assert len(items) == 4


def test_core_fields(bebop):
    item = map_mal_row(bebop)
    assert item.item_uid == "anime:mal:1"
    assert item.domain == "anime"
    assert item.source == "mal"
    assert item.title == "Cowboy Bebop"
    assert item.year == 1998
    assert item.mean_rating == 8.81
    assert item.n_ratings == 365465
    assert item.popularity == 707773.0


def test_uid_is_distinct_from_the_jikan_row_for_the_same_work(bebop):
    """Two sources describing one work stay separate rows; Phase 2 reconciles."""
    item = map_mal_row(bebop)
    assert item.item_uid == "anime:mal:1"
    assert item.external_ids["mal_id"] == "1"  # the join key Phase 2 needs


def test_mal_url_is_derived(bebop):
    # Not a column in this dump, but the URL form is stable.
    assert map_mal_row(bebop).external_ids["mal_url"] == "https://myanimelist.net/anime/1"


def test_raw_row_is_kept(bebop):
    assert map_mal_row(bebop).raw_json == bebop


# ---- popularity: the column trap -----------------------------------------


def test_popularity_uses_members_not_the_popularity_column(bebop):
    """`popularity` in this CSV is a RANK (1 = most popular).

    Mapping it onto UnifiedItem.popularity would invert every ordering built on
    it, so `members` -- the audience-size count, matching Jikan -- is used.
    """
    assert bebop["popularity"] == "38"
    assert bebop["members"] == "707773"
    assert map_mal_row(bebop).popularity == 707773.0


def test_zero_members_becomes_none(bebop):
    bebop["members"] = "0"
    assert map_mal_row(bebop).popularity is None


# ---- score ---------------------------------------------------------------


def test_unrated_row_maps_with_no_rating(rows):
    """score=0 in this dump means "never rated", not "rated zero"."""
    raw = next(r for r in rows if r["anime_id"] == "33185")
    assert raw["score"] == "0"
    item = map_mal_row(raw)
    assert item is not None
    assert item.mean_rating is None


@pytest.mark.parametrize("value", ["0", "0.00", "", "-1", "10.5", "Unknown", "abc"])
def test_unusable_scores_become_none(bebop, value):
    bebop["score"] = value
    assert map_mal_row(bebop).mean_rating is None


@pytest.mark.parametrize("value, expected", [("8.81", 8.81), ("10", 10.0), ("0.01", 0.01)])
def test_valid_scores_kept(bebop, value, expected):
    bebop["score"] = value
    assert map_mal_row(bebop).mean_rating == expected


def test_zero_scored_by_becomes_none(bebop):
    bebop["scored_by"] = "0"
    assert map_mal_row(bebop).n_ratings is None


# ---- year ----------------------------------------------------------------


def test_year_from_the_aired_dict_literal(bebop):
    """`aired` is a Python dict literal, not JSON -- json.loads would reject it."""
    assert bebop["aired"].startswith("{'from'")
    assert map_mal_row(bebop).year == 1998


def test_year_falls_back_to_premiered(bebop):
    bebop["aired"] = ""
    bebop["premiered"] = "Spring 1998"
    assert _parse_year(bebop) == 1998


def test_year_falls_back_to_aired_string(bebop):
    bebop["aired"] = ""
    bebop["premiered"] = ""
    bebop["aired_string"] = "Apr 3, 1998 to Apr 24, 1999"
    assert _parse_year(bebop) == 1998


def test_year_prefers_the_derived_column_when_present(bebop):
    # anime_cleaned.csv ships aired_from_year; the other files do not.
    bebop["aired_from_year"] = "1997"
    assert _parse_year(bebop) == 1997


def test_year_none_when_nothing_carries_it(bebop):
    for key in ("aired", "premiered", "aired_string"):
        bebop[key] = ""
    assert _parse_year(bebop) is None


def test_malformed_aired_literal_does_not_raise(bebop):
    bebop["aired"] = "{'from': not-python,,,"
    bebop["premiered"] = ""
    bebop["aired_string"] = ""
    assert _parse_year(bebop) is None


def test_implausible_year_is_not_emitted(bebop):
    for key in ("aired", "premiered", "aired_string"):
        bebop[key] = ""
    bebop["aired_from_year"] = "3500"
    assert _parse_year(bebop) is None


# ---- list columns --------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Action, Adventure", ["Action", "Adventure"]),
        ("  Action ,, Adventure  ", ["Action", "Adventure"]),
        ("Sunrise", ["Sunrise"]),
        ("", []),
        ("Unknown", []),
        ("None", []),
        (None, []),
    ],
)
def test_split_list(raw, expected):
    assert _split_list(raw) == expected


def test_tags_are_genres_plus_type(bebop):
    tags = map_mal_row(bebop).tags
    assert "action" in tags and "space" in tags
    assert "tv" in tags  # from `type`
    assert tags == [t.lower() for t in tags]  # schema normalises


def test_creators_carry_three_roles(bebop):
    creators = map_mal_row(bebop).creators
    assert ("Sunrise", "studio") in [(c.name, c.role) for c in creators]
    assert {c.role for c in creators} <= {"studio", "producer", "licensor"}


def test_row_without_studio_still_maps(rows):
    raw = next(r for r in rows if r["anime_id"] == "7518")
    assert raw["studio"] == ""
    item = map_mal_row(raw)
    assert item is not None
    assert not any(c.role == "studio" for c in item.creators)


def test_alt_titles_exclude_canonical_and_dedupe(bebop):
    item = map_mal_row(bebop)
    assert item.title not in item.alt_titles
    assert len(item.alt_titles) == len(set(item.alt_titles))


def test_japanese_title_survives_as_an_alt(bebop):
    # The encoding trap: this row is only correct if the file was read as UTF-8.
    assert "カウボーイビバップ" in map_mal_row(bebop).alt_titles


# ---- skip conditions -----------------------------------------------------


def test_row_without_id_is_skipped(bebop):
    bebop["anime_id"] = ""
    assert map_mal_row(bebop) is None


def test_row_without_any_title_is_skipped(bebop):
    for key in ("title", "title_english", "title_japanese"):
        bebop[key] = ""
    assert map_mal_row(bebop) is None


def test_title_falls_back_through_the_chain(bebop):
    bebop["title"] = "   "
    assert map_mal_row(bebop).title == bebop["title_english"]


def test_nullish_description_is_dropped(bebop):
    bebop["background"] = "Unknown"
    assert map_mal_row(bebop).description is None


# ---- the ingester ---------------------------------------------------------


@pytest.fixture
def settings(monkeypatch) -> Settings:
    monkeypatch.setenv("MAL_CSV_PATH", str(FIXTURE))
    get_settings.cache_clear()
    return get_settings()


async def test_ingester_reads_the_configured_file(settings, tmp_path):
    from recsys.db import connect, init_db

    path = tmp_path / "mal.db"
    init_db(path)
    conn = connect(path)
    try:
        result = await MalCsvAnimeIngester(settings).run(conn)
        assert result.status == "ok"
        assert result.n_fetched == 4
        assert result.n_inserted == 4
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 4
    finally:
        conn.close()


async def test_ingester_is_idempotent(settings, tmp_path):
    from recsys.db import connect, init_db

    path = tmp_path / "mal.db"
    init_db(path)
    conn = connect(path)
    try:
        await MalCsvAnimeIngester(settings).run(conn)
        result = await MalCsvAnimeIngester(settings).run(conn)
        assert (result.n_inserted, result.n_updated) == (0, 4)
    finally:
        conn.close()


async def test_target_count_limits_the_read(settings, tmp_path):
    from recsys.db import connect, init_db

    path = tmp_path / "mal.db"
    init_db(path)
    conn = connect(path)
    try:
        ingester = MalCsvAnimeIngester(settings)
        ingester.target_count = 2  # what bootstrap.py --limit sets
        result = await ingester.run(conn)
        assert result.n_fetched == 2
    finally:
        conn.close()


def test_resolve_path_rejects_a_missing_file(monkeypatch):
    monkeypatch.setenv("MAL_CSV_PATH", str(FIXTURE.parent / "nope.csv"))
    get_settings.cache_clear()
    with pytest.raises(FileNotFoundError, match="MAL_CSV_PATH"):
        MalCsvAnimeIngester(get_settings()).resolve_path()


def test_registry_exposes_the_ingester(settings):
    from recsys.ingest.registry import available, get_ingester

    assert "anime_mal_csv" in available()
    ingester = get_ingester("anime_mal_csv", settings)
    assert (ingester.domain, ingester.source) == ("anime", "mal")


def test_no_network_import_at_module_load():
    """kagglehub is imported lazily, inside resolve_path().

    Loading the registry must not drag in a network library.
    """
    import sys

    sys.modules.pop("kagglehub", None)
    import recsys.ingest.anime_mal_csv  # noqa: F401

    assert "kagglehub" not in sys.modules
