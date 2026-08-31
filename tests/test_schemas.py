"""UnifiedItem: the contract every domain collapses into.

The invariants worth defending here are the ones a future ingester could
silently violate -- a derived uid that drifts from its parts, an unnormalised
rating scale, tag casing that splits one concept into two.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from recsys.schemas import Creator, UnifiedItem


def make(**overrides) -> UnifiedItem:
    base = dict(domain="anime", source="jikan", source_id="5114", title="Cowboy Bebop")
    base.update(overrides)
    return UnifiedItem(**base)


# ---- item_uid derivation -------------------------------------------------


def test_uid_is_derived_from_parts():
    assert make().item_uid == "anime:jikan:5114"


def test_uid_matching_the_parts_is_accepted():
    assert make(item_uid="anime:jikan:5114").item_uid == "anime:jikan:5114"


def test_mismatched_explicit_uid_is_rejected():
    with pytest.raises(ValidationError, match="does not match"):
        make(item_uid="anime:jikan:9999")


def test_uid_follows_the_parts_not_the_other_way_round():
    """Two sources describing the same work stay distinct rows (Phase 2 dedups)."""
    a = make(source="jikan", source_id="1")
    b = make(source="anilist", source_id="1")
    assert a.item_uid != b.item_uid


def test_source_id_coerced_from_int():
    # Jikan hands out int ids; MusicBrainz hands out UUID strings.
    item = make(source_id=5114)
    assert item.source_id == "5114"
    assert item.item_uid == "anime:jikan:5114"


# ---- required-field validation -------------------------------------------


@pytest.mark.parametrize("field", ["source", "source_id", "title"])
def test_blank_required_fields_rejected(field):
    with pytest.raises(ValidationError):
        make(**{field: "   "})


def test_unknown_domain_rejected():
    with pytest.raises(ValidationError):
        make(domain="podcast")


def test_extra_fields_forbidden():
    # extra="forbid" keeps a renamed upstream field from being silently dropped.
    with pytest.raises(ValidationError):
        make(scoer=9.1)


# ---- numeric bounds ------------------------------------------------------


@pytest.mark.parametrize("value", [0.0, 5.5, 10.0])
def test_rating_within_scale_accepted(value):
    assert make(mean_rating=value).mean_rating == value


@pytest.mark.parametrize("value", [-0.1, 10.1, 100.0])
def test_rating_outside_0_10_rejected(value):
    # Ratings are the one cross-source comparable axis; an unrescaled 0-100
    # score leaking in would corrupt every ranking that uses it.
    with pytest.raises(ValidationError, match="normalised to 0-10"):
        make(mean_rating=value)


def test_rating_none_allowed():
    assert make(mean_rating=None).mean_rating is None


@pytest.mark.parametrize("year", [1000, 1998, 2100])
def test_plausible_years_accepted(year):
    assert make(year=year).year == year


@pytest.mark.parametrize("year", [999, 2101, 0])
def test_implausible_year_rejected(year):
    with pytest.raises(ValidationError, match="implausible year"):
        make(year=year)


def test_negative_n_ratings_rejected():
    with pytest.raises(ValidationError, match="must be >= 0"):
        make(n_ratings=-1)


def test_popularity_is_not_range_checked():
    """Popularity is a raw within-source magnitude -- 3.7M members is valid."""
    assert make(popularity=3_710_157.0).popularity == 3_710_157.0


# ---- normalisation -------------------------------------------------------


def test_tags_lowercased_stripped_and_deduped():
    item = make(tags=["Action", " action ", "ACTION", "Drama", "", "   "])
    assert item.tags == ["action", "drama"]


def test_tag_order_is_first_seen():
    assert make(tags=["Sci-Fi", "Action", "sci-fi"]).tags == ["sci-fi", "action"]


def test_alt_titles_preserve_case_but_dedupe():
    # Unlike tags, alt titles are display strings -- casing is meaningful.
    item = make(alt_titles=["Kaubōi Bibappu", " Kaubōi Bibappu ", "COWBOY BEBOP", ""])
    assert item.alt_titles == ["Kaubōi Bibappu", "COWBOY BEBOP"]


def test_external_ids_stringified_and_none_dropped():
    item = make(external_ids={"mal_id": 5114, "tmdb_id": None, "imdb": "tt123"})
    assert item.external_ids == {"mal_id": "5114", "imdb": "tt123"}


def test_creator_name_stripped_and_role_free_form():
    c = Creator(name="  Bones  ", role="studio")
    assert c.name == "Bones" and c.role == "studio"


def test_empty_creator_name_rejected():
    with pytest.raises(ValidationError):
        Creator(name="   ")


def test_naive_fetched_at_is_made_utc_aware():
    item = make(fetched_at=datetime(2024, 1, 1, 12, 0, 0))
    assert item.fetched_at.tzinfo is not None
    assert item.fetched_at.utcoffset() == timedelta(0)


def test_aware_fetched_at_converted_to_utc():
    tz = timezone(timedelta(hours=-3))  # BRT
    item = make(fetched_at=datetime(2024, 1, 1, 9, 0, 0, tzinfo=tz))
    assert item.fetched_at == datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


# ---- persistence round trip ----------------------------------------------


def test_to_row_flattens_json_columns():
    row = make(tags=["action"], alt_titles=["Alt"], external_ids={"mal_id": "1"}).to_row()
    assert row["tags"] == '["action"]'
    assert row["alt_titles"] == '["Alt"]'
    assert row["external_ids"] == '{"mal_id": "1"}'
    assert isinstance(row["fetched_at"], str)


def test_row_round_trip_preserves_the_item():
    original = make(
        alt_titles=["Kaubōi Bibappu"],
        year=1998,
        creators=[Creator(name="Sunrise", role="studio")],
        tags=["action", "space"],
        description="A ragtag crew.",
        external_ids={"mal_id": "1"},
        popularity=2_071_833.0,
        mean_rating=8.75,
        n_ratings=1_068_728,
        image_url="https://example.invalid/1.jpg",
    )
    restored = UnifiedItem.from_row(original.to_row())
    assert restored == original


def test_from_row_tolerates_missing_json_columns():
    # ITEM_COLUMNS excludes raw_json, so API-shaped rows arrive without it.
    row = make().to_row()
    row.pop("raw_json")
    assert UnifiedItem.from_row(row).raw_json is None


def test_raw_json_survives_the_round_trip():
    raw = {"mal_id": 1, "nested": {"a": [1, 2]}}
    restored = UnifiedItem.from_row(make(raw_json=raw).to_row())
    assert restored.raw_json == raw
