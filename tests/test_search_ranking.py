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


def test_an_exact_title_leads_and_the_popular_series_follows(conn):
    """"Fullmetal Alchemist" *is* the 2003 series' title, so it leads; the more
    popular Brotherhood is next, above every special that says the words more."""
    assert titles(conn, "Fullmetal Alchemist")[:2] == [
        "Fullmetal Alchemist", "Fullmetal Alchemist: Brotherhood",
    ]


def test_with_no_exact_title_the_tie_goes_to_popularity(conn):
    """Typed half way, neither series is the query: FMA (2003) fits the words
    more closely and Brotherhood is more popular, one first place each."""
    assert titles(conn, "Fullmetal Alch")[0] == "Fullmetal Alchemist: Brotherhood"


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



def built(tmp_path, rows):
    path = tmp_path / "case.db"
    init_db(path)
    c = connect(path)
    upsert_items(c, [*rows, *FILLER])
    rebuild_links(c)
    rebuild_popularity(c)
    rebuild_relations(c)
    rebuild_works(c)
    return c


def test_an_exact_title_beats_a_more_popular_prefix_match(tmp_path):
    """"gate" is GATE, not Gatekeeper -- however many more people know the latter."""
    c = built(tmp_path, [anime(1, "GATE", 50_000), anime(2, "Gatekeeper", 5_000_000),
                         anime(3, "Gatchaman", 900_000)])
    assert titles(c, "gate")[0] == "GATE"
    c.close()


def test_punctuation_splits_words_the_way_the_index_does(tmp_path):
    """"Steins;Gate" is two words, as FTS5 reads it; counting spaces made it one
    and put it above "Gate" for the query "gate"."""
    c = built(tmp_path, [anime(1, "Steins;Gate", 2_500_000), anime(2, "Gate", 10_000)])
    assert titles(c, "gate") == ["Gate", "Steins;Gate"]
    c.close()


def test_fewer_title_words_rank_better_when_nothing_is_exact(tmp_path):
    """No title is "fate zer"; the shortest one holding both words fits best.
    Without the word count all three tie on text and popularity alone orders
    them C, B, A."""
    c = built(tmp_path, [
        anime(1, "Fate/Zero", 300_000),
        anime(2, "Fate/Zero Specials", 600_000),
        anime(3, "Fate/Zero Remix Recap Collection Box", 900_000),
    ])
    got = titles(c, "fate zer")
    assert got.index("Fate/Zero") < got.index("Fate/Zero Specials")
    c.close()


def test_popularity_is_ranked_within_each_domain(tmp_path):
    """Three composers whose Deezer fan counts dwarf any anime's MAL members,
    and one anime: compared on one scale the anime would rank last for
    nothing but the size of another service's numbers."""
    c = built(tmp_path, [
        anime(1, "Gat Show", 1_000),
        *[UnifiedItem(domain="music", source="mb", source_id=str(n), title=f"Gat Band{n}",
                      popularity=float(9e9 - n)) for n in range(3)],
    ])
    assert "Gat Show" in titles(c, "gat")[:2]
    c.close()


def test_without_the_read_view_search_still_uses_calibrated_popularity(tmp_path):
    """The fallback reads popularity_norm, not the raw count."""
    path = tmp_path / "fallback.db"
    init_db(path)
    c = connect(path)
    other = UnifiedItem(domain="anime", source="other", source_id="2", title="Monster",
                        popularity=1_000.0)
    upsert_items(c, [anime(1, "Monster", 10_000_000), other])
    rebuild_links(c)
    rebuild_popularity(c)
    with c:
        c.execute("UPDATE popularity_norm SET value = 10 WHERE item_uid = 'anime:anilist:1'")
        c.execute("UPDATE popularity_norm SET value = 5000 WHERE item_uid = 'anime:other:2'")
        c.execute("DELETE FROM works")
    assert next(i.item_uid for i in query_items(c, q="Monster")[1]) == "anime:other:2"
    c.close()


def test_a_series_is_found_by_its_single_volume_not_its_boxed_set(tmp_path):
    from recsys.schemas import Creator

    def book(n, title, pop):
        return UnifiedItem(domain="book", source="goodbooks", source_id=str(n), title=title,
                           popularity=float(pop), creators=[Creator(name="J.K. Rowling")])

    c = built(tmp_path, [
        book(1, "Harry Potter Boxset (Harry Potter, #1-7)", 190_000),
        book(2, "Harry Potter and the Sorcerer's Stone (Harry Potter, #1)", 4_600_000),
        book(3, "Harry Potter and the Chamber of Secrets (Harry Potter, #2)", 1_800_000),
    ])
    assert titles(c, "harry potter") == [
        "Harry Potter and the Sorcerer's Stone (Harry Potter, #1)",
    ]
    c.close()
