"""What a search puts first, and what a score filter lets through.

Both were wrong in a way that only showed on the real catalog. Typing
"shingeki no kyojin" opened on a recap special rather than the series: bm25
measures how much of a document the words are, and a row with no synopsis is
nearly all title. And a work scoring 7.96 was printed "8.0" on its card and
then left out of "8 and up".
"""

from __future__ import annotations

import pytest

from recsys.db import connect, init_db, query_items, upsert_items
from recsys.schemas import UnifiedItem, utcnow
from recsys.works import rebuild_works


def item(source_id, title, **kw):
    kw.setdefault("domain", "anime")
    return UnifiedItem(source="anilist", source_id=str(source_id), title=title,
                       fetched_at=utcnow(), **kw)


SYNOPSIS = ("Several hundred years ago, humans were nearly exterminated by titans. "
            "Eren vows revenge on every one of them. ") * 6

CATALOG = [
    # The recap: no synopsis, so the query is most of the document. Obscure.
    item(1, "Shingeki no Kyojin: Ano Hi Kara", popularity=30.0,
         alt_titles=["Attack on Titan: Since That Day"]),
    # The series: the title *is* the query, under a long synopsis. Famous.
    item(2, "Shingeki no Kyojin", popularity=900.0, description=SYNOPSIS,
         alt_titles=["Attack on Titan", "進撃の巨人"]),
    item(3, "Shingeki no Kyojin Season 2", popularity=700.0, description=SYNOPSIS,
         alt_titles=["Attack on Titan Season 2"]),
    # Mentions the words, is called something else, and is the most popular.
    item(4, "Giant Documentary", popularity=5000.0,
         description="How Shingeki no Kyojin became an attack on titan-sized hit."),
]


@pytest.fixture(params=["live joins", "read view"])
def conn(request, tmp_path):
    init_db(tmp_path / "s.db")
    c = connect(tmp_path / "s.db")
    upsert_items(c, CATALOG)
    if request.param == "read view":
        rebuild_works(c)
    yield c
    c.close()


def titles(conn, **kw):
    return [i.title for i in query_items(conn, **kw)[1]]


def test_the_work_called_exactly_that_comes_first(conn):
    assert titles(conn, q="shingeki no kyojin")[0] == "Shingeki no Kyojin"


def test_an_alternative_title_is_as_exact_as_the_title(conn):
    """Most people type the English name; it is stored as an alt title."""
    assert titles(conn, q="attack on titan")[0] == "Shingeki no Kyojin"
    assert titles(conn, q="ATTACK ON TITAN")[0] == "Shingeki no Kyojin"


def test_named_works_are_ordered_by_how_well_known_they_are(conn):
    assert titles(conn, q="shingeki no kyojin")[:3] == [
        "Shingeki no Kyojin", "Shingeki no Kyojin Season 2",
        "Shingeki no Kyojin: Ano Hi Kara",
    ]


def test_a_mention_in_a_synopsis_never_outranks_a_name(conn):
    """However popular the thing doing the mentioning is."""
    assert titles(conn, q="shingeki no kyojin")[-1] == "Giant Documentary"


def test_typing_part_of_a_name_already_finds_the_main_work(conn):
    """No exact match yet, so it falls to the best-known thing by that name."""
    assert titles(conn, q="shingeki no ky")[0] == "Shingeki no Kyojin"


def test_a_search_that_only_a_synopsis_answers_still_answers(conn):
    assert titles(conn, q="exterminated")[:2] == [
        "Shingeki no Kyojin", "Shingeki no Kyojin Season 2",
    ]


def test_an_explicit_sort_still_overrides_relevance(conn):
    assert titles(conn, q="shingeki", sort="title")[0] == "Giant Documentary"


def test_the_total_is_unchanged_by_the_ordering(conn):
    assert query_items(conn, q="shingeki no kyojin")[0] == 4


def test_searching_for_fts_syntax_is_still_just_words(conn):
    assert query_items(conn, q='NEAR( "* OR') == (0, [])


# ---- the score filter ----------------------------------------------------


def test_a_score_filter_means_the_number_printed_on_the_card(conn):
    """7.96 prints as 8.0, so "8 and up" includes it; 7.94 prints as 7.9."""
    upsert_items(conn, [
        item(10, "Prints As Eight", mean_rating=7.96, n_ratings=100),
        item(11, "Prints As Seven Nine", mean_rating=7.94, n_ratings=100),
        item(12, "Exactly Eight", mean_rating=8.0, n_ratings=100),
    ])
    if conn.execute("SELECT EXISTS(SELECT 1 FROM works)").fetchone()[0]:
        rebuild_works(conn)
    assert sorted(titles(conn, min_rating=8)) == ["Exactly Eight", "Prints As Eight"]
