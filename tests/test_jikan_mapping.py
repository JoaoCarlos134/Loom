"""Jikan record -> UnifiedItem.

``map_jikan_anime`` is pure, so the whole mapping surface is testable against
the saved fixture with no network. The fixture holds three real records
(Fullmetal Alchemist: Brotherhood, Steins;Gate, Cowboy Bebop).
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from recsys.ingest.anime_jikan import (
    _clean_text,
    _pick_image,
    _pick_year,
    map_jikan_anime,
)


@pytest.fixture
def fma(jikan_records) -> dict:
    """Fullmetal Alchemist: Brotherhood -- the fully-populated record."""
    return copy.deepcopy(jikan_records[0])


# ---- the happy path ------------------------------------------------------


def test_every_fixture_record_maps(jikan_records):
    items = [map_jikan_anime(r) for r in jikan_records]
    assert all(i is not None for i in items)
    assert [i.item_uid for i in items] == [
        "anime:jikan:5114",
        "anime:jikan:9253",
        "anime:jikan:1",
    ]


def test_core_fields_mapped(fma):
    item = map_jikan_anime(fma)
    assert item.domain == "anime"
    assert item.source == "jikan"
    assert item.source_id == "5114"
    assert item.title == "Fullmetal Alchemist: Brotherhood"
    assert item.year == 2009
    assert item.mean_rating == 9.11
    assert item.n_ratings == 2_326_893
    assert item.popularity == 3_710_157.0


def test_raw_json_is_carried_verbatim(fma):
    # The point of raw_json: a mapping bug is fixable offline.
    assert map_jikan_anime(fma).raw_json == fma


def test_fetched_at_override_is_used(fma):
    stamp = datetime(2024, 5, 1, tzinfo=timezone.utc)
    assert map_jikan_anime(fma, fetched_at=stamp).fetched_at == stamp


# ---- synopsis cleaning ---------------------------------------------------


def test_mal_footer_stripped_from_synopsis(fma):
    assert fma["synopsis"].endswith("[Written by MAL Rewrite]")
    description = map_jikan_anime(fma).description
    assert "[Written by MAL Rewrite]" not in description
    assert description.endswith("threatens the fate of the world.")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Plot. [Written by MAL Rewrite]", "Plot."),
        ("Plot. [Source: ANN]", "Plot."),
        ("Plot. [source: manga]", "Plot."),  # case-insensitive
        ("Plot.  [Written by X]  ", "Plot."),
        ("Plot with no footer.", "Plot with no footer."),
        ("A [bracketed] aside stays.", "A [bracketed] aside stays."),
        ("", None),
        ("   ", None),
        ("[Written by MAL Rewrite]", None),  # footer-only -> nothing left
        (None, None),
        (123, None),
    ],
)
def test_clean_text_cases(raw, expected):
    assert _clean_text(raw) == expected


def test_missing_synopsis_gives_none(fma):
    fma.pop("synopsis")
    assert map_jikan_anime(fma).description is None


# ---- year fallback -------------------------------------------------------


def test_year_falls_back_to_aired_prop(fma):
    """Jikan leaves `year` null on plenty of records but fills `aired.prop`."""
    fma["year"] = None
    assert fma["aired"]["prop"]["from"]["year"] == 2009
    assert map_jikan_anime(fma).year == 2009


def test_year_none_when_both_sources_absent(fma):
    fma["year"] = None
    fma["aired"] = {}
    assert map_jikan_anime(fma).year is None


@pytest.mark.parametrize("bad", [0, 999, 2101, "2009", None])
def test_implausible_top_level_year_ignored(fma, bad):
    """A junk `year` must fall through to the fallback, not raise."""
    fma["year"] = bad
    assert _pick_year(fma) == 2009


def test_implausible_year_everywhere_gives_none(fma):
    fma["year"] = 3000
    fma["aired"]["prop"]["from"]["year"] = 12
    assert _pick_year(fma) is None


# ---- images --------------------------------------------------------------


def test_image_prefers_large_jpg(fma):
    assert map_jikan_anime(fma).image_url == fma["images"]["jpg"]["large_image_url"]


def test_image_falls_back_through_formats():
    raw = {"images": {"jpg": {}, "webp": {"small_image_url": "https://x.invalid/s.webp"}}}
    assert _pick_image(raw) == "https://x.invalid/s.webp"


@pytest.mark.parametrize("images", [{}, {"jpg": {}}, {"jpg": {"large_image_url": "  "}}, None])
def test_missing_image_gives_none(images):
    assert _pick_image({"images": images}) is None


def test_item_maps_without_images(fma):
    fma.pop("images")
    assert map_jikan_anime(fma).image_url is None


# ---- score ---------------------------------------------------------------


def test_missing_score_gives_none(fma):
    fma["score"] = None
    item = map_jikan_anime(fma)
    assert item.mean_rating is None


def test_out_of_scale_score_dropped_not_raised(fma):
    """A bad upstream score must skip the field, not kill the record."""
    fma["score"] = 91.1
    item = map_jikan_anime(fma)
    assert item is not None
    assert item.mean_rating is None


def test_non_numeric_score_dropped(fma):
    fma["score"] = "9.11"
    assert map_jikan_anime(fma).mean_rating is None


def test_non_int_scored_by_dropped(fma):
    fma["scored_by"] = None
    assert map_jikan_anime(fma).n_ratings is None


# ---- skip conditions -----------------------------------------------------


def test_record_without_mal_id_is_skipped(fma):
    fma["mal_id"] = None
    assert map_jikan_anime(fma) is None


def test_record_without_any_title_is_skipped(fma):
    for key in ("title", "title_english", "title_japanese"):
        fma[key] = None
    fma["titles"] = []
    assert map_jikan_anime(fma) is None


def test_blank_title_falls_back_to_english(fma):
    fma["title"] = "   "
    fma["title_english"] = "FMA: Brotherhood"
    assert map_jikan_anime(fma).title == "FMA: Brotherhood"


def test_title_falls_back_to_default_entry_in_titles(fma):
    for key in ("title", "title_english", "title_japanese"):
        fma[key] = None
    assert map_jikan_anime(fma).title == "Fullmetal Alchemist: Brotherhood"


# ---- tags and creators ---------------------------------------------------


def test_tags_merge_all_four_label_arrays_plus_type(fma):
    tags = map_jikan_anime(fma).tags
    assert "action" in tags
    assert "shounen" in tags        # demographics
    assert fma["type"].lower() in tags  # "tv"


def test_tags_are_normalised_by_the_schema(fma):
    tags = map_jikan_anime(fma).tags
    assert tags == [t.lower() for t in tags]
    assert len(tags) == len(set(tags))


def test_missing_label_arrays_are_tolerated(fma):
    for key in ("genres", "explicit_genres", "themes", "demographics"):
        fma[key] = None
    assert map_jikan_anime(fma).tags == ["tv"]


def test_creators_carry_studio_and_producer_roles(fma):
    creators = map_jikan_anime(fma).creators
    roles = {c.role for c in creators}
    assert "studio" in roles
    assert any(c.name == "Bones" and c.role == "studio" for c in creators)
    assert roles <= {"studio", "producer"}


def test_malformed_creator_entries_are_dropped(fma):
    fma["studios"] = [{"name": "Bones"}, {"name": ""}, {"noname": 1}, "junk", None]
    names = [c.name for c in map_jikan_anime(fma).creators if c.role == "studio"]
    assert names == ["Bones"]


# ---- titles and ids ------------------------------------------------------


def test_alt_titles_exclude_the_canonical_title(fma):
    item = map_jikan_anime(fma)
    assert item.title not in item.alt_titles


def test_alt_titles_include_synonyms_and_japanese(fma):
    alts = map_jikan_anime(fma).alt_titles
    assert "FMAB" in alts
    assert fma["title_japanese"] in alts


def test_alt_titles_deduped(fma):
    alts = map_jikan_anime(fma).alt_titles
    assert len(alts) == len(set(alts))


def test_external_ids_capture_mal_and_url(fma):
    ids = map_jikan_anime(fma).external_ids
    assert ids["mal_id"] == "5114"
    assert ids["mal_url"] == fma["url"]


def test_youtube_id_captured_when_present(fma):
    fma["trailer"] = {"youtube_id": "--IcmZkvL0Q"}
    assert map_jikan_anime(fma).external_ids["youtube_id"] == "--IcmZkvL0Q"


def test_absent_trailer_omits_youtube_id(fma):
    fma["trailer"] = {"youtube_id": None}
    assert "youtube_id" not in map_jikan_anime(fma).external_ids
