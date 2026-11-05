"""Search ranks the work people mean, not the entry that repeats the words most.

Measured on a full build: searching "Fullmetal Alchemist" put *Brotherhood*
fourth, and "Steins;Gate" put the series below four of its specials. Search
ordered by bm25 alone, and bm25 rewards repetition -- a special's synopsis says
"Steins;Gate" three times where the series' own synopsis is about a lab.

The fix fuses two rankings by reciprocal rank (Cormack, Clarke & Buettcher,
2009), which needs no scale shared between them: how well the title matches,
and the calibrated popularity -- ``works.sort_key``, never raw ``popularity``.
These fixtures reproduce both failures under bm25 alone.
"""

from __future__ import annotations

import pytest

from recsys.db import connect, init_db, query_items, upsert_items
from recsys.link import rebuild_links
from recsys.popularity import rebuild_popularity
from recsys.relations import rebuild_relations
from recsys.schemas import UnifiedItem
from recsys.works import rebuild_works


def anime(source_id, title, popularity, description=""):
    return UnifiedItem(
        domain="anime", source="anilist", source_id=str(source_id), title=title,
        popularity=float(popularity), description=description or None,
    )


FMA = [
    anime(121, "Fullmetal Alchemist", 900_000,
          "Edward and Alphonse Elric try to bring their mother back."),
    anime(5114, "Fullmetal Alchemist: Brotherhood", 3_000_000,
          "The brothers search for the Philosopher's Stone."),
    anime(6421, "Fullmetal Alchemist: Brotherhood Specials", 120_000,
          "Fullmetal Alchemist shorts. Fullmetal Alchemist side stories of "
          "Fullmetal Alchemist."),
    anime(908, "Fullmetal Alchemist: Premium Collection", 60_000,
          "Fullmetal Alchemist extras from the Fullmetal Alchemist DVDs."),
    anime(430, "Fullmetal Alchemist: Reflections", 50_000,
          "A Fullmetal Alchemist recap of Fullmetal Alchemist."),
    anime(9135, "Fullmetal Alchemist: The Sacred Star of Milos", 200_000,
          "A Fullmetal Alchemist film."),
]

STEINS_GATE = [
    anime(9253, "Steins;Gate", 2_500_000, "A self-proclaimed mad scientist and his lab."),
    anime(10863, "Steins;Gate: Oukoubakko no Poriomania", 150_000,
          "A Steins;Gate special. Steins;Gate characters, Steins;Gate again."),
    anime(11577, "Steins;Gate Movie: Fuka Ryouiki no Deja vu", 400_000,
          "The Steins;Gate film, a year after Steins;Gate."),
    anime(30484, "Steins;Gate 0", 900_000, "Steins;Gate's other world line."),
    anime(14833, "Steins;Gate: Kyoukaimenjou no Missing Link", 140_000,
          "A Steins;Gate episode. Steins;Gate, Steins;Gate."),
    anime(32188, "Steins;Gate: Kyoukaimenjou no Missing Link - Divide By Zero", 90_000,
          "Steins;Gate special. Steins;Gate."),
]


#: The rest of a catalog. bm25 weighs a word by how rare it is, so a corpus
#: where every document says "Steins;Gate" scores every match the same; the
#: real one holds 84k works and these words in a handful of them.
FILLER = [
    anime(100_000 + n, f"Unrelated Show {n}", 1_000 + n,
          f"A story about {word} and the people around it."
          if (word := ("ships", "cooking", "school", "war", "music")[n % 5]) else "")
    for n in range(300)
]


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "search.db"
    init_db(path)
    c = connect(path)
    upsert_items(c, [*FMA, *STEINS_GATE, *FILLER])
    rebuild_links(c)
    rebuild_popularity(c)
    rebuild_relations(c)
    rebuild_works(c)
    yield c
    c.close()


def titles(conn, q, **kw):
    return [i.title for i in query_items(conn, q=q, **kw)[1]]


def test_the_two_series_lead_a_fullmetal_alchemist_search(conn):
    got = titles(conn, "Fullmetal Alchemist")
    assert set(got[:2]) == {"Fullmetal Alchemist", "Fullmetal Alchemist: Brotherhood"}


def test_brotherhood_is_first_when_the_rankings_disagree_evenly(conn):
    """FMA (2003) matches the query more exactly and Brotherhood is more
    popular: one first place each. The tie goes to popularity."""
    assert titles(conn, "Fullmetal Alchemist")[0] == "Fullmetal Alchemist: Brotherhood"


def test_the_series_leads_its_own_specials(conn):
    assert titles(conn, "Steins;Gate")[0] == "Steins;Gate"


def test_a_specific_special_is_still_findable_by_its_own_name(conn):
    assert titles(conn, "Poriomania")[0] == "Steins;Gate: Oukoubakko no Poriomania"
    assert titles(conn, "Divide By Zero")[0].endswith("Divide By Zero")


def test_a_description_only_match_still_ranks(conn):
    """No title contains 'mad scientist'; the description match must survive."""
    assert titles(conn, "mad scientist") == ["Steins;Gate"]


def test_paging_through_search_is_stable(conn):
    everything = titles(conn, "Steins;Gate", limit=50)
    paged = titles(conn, "Steins;Gate", limit=2) + titles(conn, "Steins;Gate", limit=2, offset=2) \
        + titles(conn, "Steins;Gate", limit=2, offset=4)
    assert paged == everything
    assert query_items(conn, q="Steins;Gate")[0] == len(STEINS_GATE)


def test_search_without_the_read_view_still_works(tmp_path):
    """An unlinked catalog has no `works` yet; search must degrade, not fail."""
    path = tmp_path / "fresh.db"
    init_db(path)
    c = connect(path)
    upsert_items(c, [*STEINS_GATE, *FILLER])
    assert titles(c, "Steins;Gate")[0] == "Steins;Gate"
    c.close()


def test_the_calibrated_popularity_decides_never_the_raw_count(tmp_path):
    """Two works the query fits equally. The raw counts say A, the calibrated
    values say B -- raw counts mean something different in every source."""
    path = tmp_path / "raw.db"
    init_db(path)
    c = connect(path)
    other = UnifiedItem(domain="anime", source="other", source_id="2", title="Monster",
                        popularity=1_000.0)
    upsert_items(c, [anime(1, "Monster", 10_000_000), other, *FILLER])
    rebuild_links(c)
    rebuild_popularity(c)
    with c:
        c.execute("UPDATE popularity_norm SET value = 10 WHERE item_uid = 'anime:anilist:1'")
        c.execute("UPDATE popularity_norm SET value = 5000 WHERE item_uid = 'anime:other:2'")
    rebuild_relations(c)
    rebuild_works(c)
    assert [i.item_uid for i in query_items(c, q="Monster")[1]] == [
        "anime:other:2", "anime:anilist:1",
    ]
    c.close()


def test_a_match_only_in_alternative_titles_is_still_found(conn):
    upsert_items(conn, [anime(16498, "Attack on Titan", 3_500_000, "Walls.").model_copy(
        update={"alt_titles": ["Shingeki no Kyojin"]})])
    rebuild_works(conn)
    assert titles(conn, "Shingeki no Kyojin") == ["Attack on Titan"]
