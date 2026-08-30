"""Removing rows that today's ingest filters would reject.

This is the only code in Loom that deletes anything. Every other write is an
upsert, `link.py` hides duplicates without touching them, and `popularity.py`
stores a second opinion beside the source datum. So the tests here care as much
about what is *kept* and about the dry run being honest as about what goes.
"""

from __future__ import annotations

import pytest

from recsys.config import Settings, get_settings
from recsys.db import connect, count_rows, init_db, upsert_items
from recsys.prune import prune
from recsys.schemas import UnifiedItem


@pytest.fixture
def settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "prune.db"
    init_db(path)
    c = connect(path)
    yield c
    c.close()


def item(domain, source_id, *, n_ratings=None, raw=None, source="src", title="Thing"):
    return UnifiedItem(
        domain=domain, source=source, source_id=str(source_id), title=title,
        n_ratings=n_ratings, popularity=float(n_ratings) if n_ratings else None,
        raw_json=raw,
    )


# ---- what goes ------------------------------------------------------------


def test_a_film_below_the_vote_floor_goes(conn, settings):
    upsert_items(conn, [item("movie", 1, n_ratings=3)])
    assert prune(conn, settings, dry_run=False).deleted == 1
    assert count_rows(conn) == 0


def test_a_show_is_judged_on_the_television_floor(conn, settings):
    """TV vote counts run an order of magnitude below film's, so a show with 30
    votes stays while a film with 30 votes goes."""
    votes = settings.tmdb_tv_min_votes + 5
    assert votes < settings.tmdb_min_votes  # the premise of this test
    upsert_items(conn, [item("tv", 1, n_ratings=votes), item("movie", 2, n_ratings=votes)])
    prune(conn, settings, dry_run=False)
    survivors = {r[0] for r in conn.execute("SELECT item_uid FROM items")}
    assert survivors == {"tv:src:1"}


def test_adult_titles_go_from_any_domain(conn, settings):
    upsert_items(conn, [
        item("anime", 1, n_ratings=9999, raw={"isAdult": True}),
        item("movie", 2, n_ratings=9999, raw={"adult": True}),
    ])
    assert prune(conn, settings, dry_run=False).deleted == 2


def test_music_videos_filed_as_anime_go(conn, settings):
    """AniList lists opening-theme clips as anime. They are not works."""
    upsert_items(conn, [item("anime", 1, n_ratings=99999, raw={"format": "MUSIC"})])
    assert prune(conn, settings, dry_run=False).deleted == 1


def test_a_row_failing_two_rules_is_deleted_once(conn, settings):
    upsert_items(conn, [item("movie", 1, n_ratings=1, raw={"adult": True})])
    result = prune(conn, settings, dry_run=False)
    assert result.deleted == 1
    assert result.total > result.deleted  # counted under both reasons


# ---- what stays -----------------------------------------------------------


def test_a_well_rated_old_film_stays(conn, settings):
    """Nothing here judges age. An obscure 1927 film with ten thousand votes is
    a better entry than a forgotten 2019 one with three."""
    upsert_items(conn, [item("movie", 1, n_ratings=10_000, title="Metropolis")])
    assert prune(conn, settings, dry_run=False).deleted == 0
    assert count_rows(conn) == 1


def test_domains_without_a_vote_rule_are_untouched(conn, settings):
    """Books come pre-curated -- the whole dump is the 10k most-rated -- and
    music artists carry no rating at all. Neither has a floor to fail."""
    upsert_items(conn, [item("book", 1), item("music", 2), item("manga", 3)])
    assert prune(conn, settings, dry_run=False).deleted == 0


def test_a_missing_adult_flag_is_not_read_as_adult(conn, settings):
    """The older MAL rows predate the field. Absent must not mean true."""
    upsert_items(conn, [item("anime", 1, n_ratings=500, raw={"title": "no flag"})])
    assert prune(conn, settings, dry_run=False).deleted == 0


def test_a_row_with_no_raw_json_survives(conn, settings):
    upsert_items(conn, [item("anime", 1, n_ratings=500, raw=None)])
    assert prune(conn, settings, dry_run=False).deleted == 0


# ---- the dry run has to be honest -----------------------------------------


def test_a_dry_run_deletes_nothing(conn, settings):
    upsert_items(conn, [item("movie", i, n_ratings=1) for i in range(5)])
    result = prune(conn, settings)  # dry_run defaults to True
    assert result.dry_run and result.deleted == 0
    assert count_rows(conn) == 5


def test_the_dry_run_count_matches_what_deletion_removes(conn, settings):
    upsert_items(conn, [
        *[item("movie", i, n_ratings=1) for i in range(5)],
        *[item("movie", 100 + i, n_ratings=99_999) for i in range(3)],
    ])
    predicted = prune(conn, settings).counts["movie: fewer than %d votes"
                                             % settings.tmdb_min_votes]
    actual = prune(conn, settings, dry_run=False).deleted
    assert predicted == actual == 5
    assert count_rows(conn) == 3


def test_pruning_an_already_clean_catalog_is_a_noop(conn, settings):
    upsert_items(conn, [item("movie", 1, n_ratings=99_999)])
    assert prune(conn, settings, dry_run=False).deleted == 0
    assert prune(conn, settings, dry_run=False).deleted == 0


def test_pruning_an_empty_catalog_is_a_noop(conn, settings):
    assert prune(conn, settings, dry_run=False).deleted == 0
