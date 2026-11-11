"""Same-franchise suppression: the walk over relation edges, and the title rule.

The cases here are the ones measured on the real catalog. Rating *DEATH NOTE*
filled 21 of 40 recommendation slots with more *Death Note*; the first fix for
that hid *ONE PIECE* from anyone who rated *Dragon Ball Z*; the second hid the
*MONSTER* manga from anyone who rated *Golden Kamuy*. Each of those is a test
below, so the next person to loosen a rule finds out what it was holding back.
"""

from __future__ import annotations

import math
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from recsys.api.routes import _load_collaborative_index, _load_similarity_index
from recsys.config import get_settings
from recsys.db import connect, init_db, set_rating, upsert_items
from recsys.franchise import (
    Franchise,
    base_titles,
    extended_titles,
    franchise_of,
    title_key,
)
from recsys.link import rebuild_links
from recsys.relations import franchise_representatives, rebuild_relations
from recsys.schemas import UnifiedItem, utcnow
from tests.test_similar_route import write_matrix


def anilist(source_id, title, *, domain="anime", edges=(), alt_titles=()):
    return UnifiedItem(
        domain=domain, source="anilist", source_id=str(source_id), title=title,
        alt_titles=list(alt_titles), fetched_at=utcnow(),
        raw_json={"id": source_id, "relations": {"edges": list(edges)}},
    )


def other(source, source_id, title, *, domain, alt_titles=()):
    """A row from a source that asserts no relations at all."""
    return UnifiedItem(
        domain=domain, source=source, source_id=str(source_id), title=title,
        alt_titles=list(alt_titles), fetched_at=utcnow(),
    )


def edge(node_id, relation, node_type="ANIME"):
    return {"relationType": relation, "node": {"id": node_id, "type": node_type}}


def uid(source_id, domain="anime"):
    return f"{domain}:anilist:{source_id}"


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "f.db")
    c = connect(tmp_path / "f.db")
    yield c
    c.close()


def load(conn, items):
    upsert_items(conn, items)
    rebuild_links(conn)
    rebuild_relations(conn)


# ---- the walk ------------------------------------------------------------


def test_the_walk_is_transitive(conn):
    """AniList joins neighbours only: season three is an edge from season two
    and nothing from season one. One hop would recommend it back to you."""
    load(conn, [
        anilist(1, "Show", edges=[edge(2, "SEQUEL")]),
        anilist(2, "Show II", edges=[edge(3, "SEQUEL")]),
        anilist(3, "Show III"),
    ])
    assert franchise_representatives(conn, uid(1)) == {uid(2), uid(3)}


def test_the_same_story_in_another_medium_is_franchise(conn):
    """The manga an anime adapts, and that manga's own side stories two edges
    away -- the Death Note one-shots nothing reached before."""
    load(conn, [
        anilist(1, "Note", edges=[edge(50, "ADAPTATION", "MANGA")]),
        anilist(50, "Note", domain="manga", edges=[edge(51, "SIDE_STORY", "MANGA")]),
        anilist(51, "Note: One-Shot", domain="manga"),
    ])
    assert franchise_representatives(conn, uid(1)) == {uid(50, "manga"), uid(51, "manga")}


def test_a_work_is_not_in_its_own_franchise(conn):
    """Edges run both ways, so the walk always arrives back where it started."""
    load(conn, [anilist(1, "Show", edges=[edge(2, "SEQUEL")]),
                anilist(2, "Show II", edges=[edge(1, "PREQUEL")])])
    assert uid(1) not in franchise_representatives(conn, uid(1))


def test_a_shared_character_does_not_join_two_franchises(conn):
    """Measured: following CHARACTER edges merges 4,599 works into one group."""
    load(conn, [anilist(1, "Show", edges=[edge(9, "CHARACTER")]), anilist(9, "Other Show")])
    assert franchise_representatives(conn, uid(1)) == set()


def test_the_walk_does_not_pass_through_a_crossover(conn):
    """*Dragon Ball Z* reached *ONE PIECE* through a festival short filed under
    both. The short belongs to each franchise and joins neither to the other."""
    load(conn, [
        anilist(1, "Dragon Saga"),
        anilist(2, "Pirate Saga", edges=[edge(3, "SEQUEL")]),
        anilist(3, "Pirate Saga II"),
        anilist(7, "Dragon x Pirate Special", edges=[edge(1, "PARENT"), edge(2, "PARENT")]),
    ])
    assert franchise_representatives(conn, uid(1)) == {uid(7)}
    assert franchise_representatives(conn, uid(2)) == {uid(3), uid(7)}


def test_a_crossover_named_from_the_parents_side_is_still_one(conn):
    """AniList often records only one end: the series names its SIDE_STORY and
    the special names nothing. Two parents either way."""
    load(conn, [
        anilist(1, "Dragon Saga", edges=[edge(7, "SIDE_STORY")]),
        anilist(2, "Pirate Saga", edges=[edge(7, "SPIN_OFF"), edge(3, "SEQUEL")]),
        anilist(3, "Pirate Saga II"),
        anilist(7, "Dragon x Pirate Special"),
    ])
    assert franchise_representatives(conn, uid(1)) == {uid(7)}


def test_rating_the_crossover_itself_reaches_both_sides(conn):
    """The query is always walked from: you do know both things it crosses."""
    load(conn, [
        anilist(1, "Dragon Saga"),
        anilist(2, "Pirate Saga"),
        anilist(7, "Dragon x Pirate Special", edges=[edge(1, "PARENT"), edge(2, "PARENT")]),
    ])
    assert franchise_representatives(conn, uid(7)) == {uid(1), uid(2)}


def test_two_parents_in_one_franchise_are_not_a_crossover(conn):
    """An OVA set between two seasons is derived from both. It is held like a
    crossover on arrival and let through once both parents are known to be
    this franchise -- whichever edge the walk happened to follow first."""
    load(conn, [
        anilist(1, "Show", edges=[edge(2, "SEQUEL"), edge(4, "SIDE_STORY")]),
        anilist(2, "Show II", edges=[edge(4, "SIDE_STORY")]),
        anilist(4, "Show OVA", edges=[edge(5, "SEQUEL")]),
        anilist(5, "Show OVA 2"),
    ])
    assert franchise_representatives(conn, uid(1)) == {uid(2), uid(4), uid(5)}
    assert franchise_representatives(conn, uid(2)) == {uid(1), uid(4), uid(5)}


def test_a_side_story_with_one_parent_is_walked_through(conn):
    """Only *two* parents make a crossover. An ordinary OVA is a way onward."""
    load(conn, [
        anilist(1, "Show", edges=[edge(4, "SIDE_STORY")]),
        anilist(4, "Show OVA", edges=[edge(5, "SEQUEL")]),
        anilist(5, "Show OVA 2"),
    ])
    assert franchise_representatives(conn, uid(1)) == {uid(4), uid(5)}


# ---- the title rule ------------------------------------------------------


def test_titles_are_compared_without_case_or_accents():
    assert title_key("Pokémon") == title_key("POKEMON")
    assert title_key("DEATH NOTE") == ("death", "note")


@pytest.mark.parametrize("title, head", [
    ("Death Note: The Last Name", ("death", "note")),
    ("Death Note, Vol. 1", ("death", "note")),
    ("Death Note Relight 1: Visions of a God", ("death", "note")),
    ("Attack on Titan Season 2", ("attack", "on", "titan")),
    ("Monster: The Movie", ("monster",)),
    ("Alien 3", ("alien",)),
    ("Overlord II", ("overlord",)),
    ("Fate/Zero", ("fate",)),
    ("Bleach, Vol. 1", ("bleach",)),
])
def test_a_longer_title_extends_a_shorter_one(title, head):
    assert head in extended_titles(title)


@pytest.mark.parametrize("title, head", [
    ("Monster House", ("monster",)),          # a longer name, not an entry
    ("Alien Nation", ("alien",)),
    ("Love, Stargirl", ("love",)),            # a comma is as often a list
    ("Hunter x Hunter", ("hunter",)),         # not the tenth Hunter
    ("The Boy and the Heron", ("the", "boy")),  # an article is not a word
])
def test_a_one_word_title_needs_more_than_a_space_after_it(title, head):
    assert head not in extended_titles(title)


def test_a_subtitle_is_cut_to_find_what_a_work_is_an_entry_of():
    assert ("attack", "on", "titan") in base_titles("Attack on Titan Season 2")
    assert ("fullmetal", "alchemist") in base_titles("Fullmetal Alchemist: Brotherhood")


@pytest.mark.parametrize("title, stem", [
    ("Attack on Titan", ("attack", "on")),             # no mark, so no cut
    ("Death Note Relight", ("death",)),
    ("Mission: Yozakura Family", ("mission",)),        # hid Mission: Impossible
    ("Fate/Zero", ("fate",)),                          # hid Fate: The Winx Saga
    ("The 100 Girlfriends", ("the",)),                 # hid The 100
])
def test_a_cut_never_leaves_a_one_word_stem(title, stem):
    """Measured: one-word stems are where the collisions were."""
    assert stem not in base_titles(title)


def test_an_exact_title_match_is_covered():
    f = Franchise(titles={("death", "note")})
    assert f.covers(other("tmdb", 1, "Death Note", domain="movie"))
    assert f.covers(other("tmdb", 2, "DEATH NOTE", domain="tv"))


def test_a_candidate_is_matched_by_its_other_names_too():
    """A film listed under its English title, the anime under its romaji."""
    f = Franchise(titles={("shingeki", "no", "kyojin")})
    film = other("tmdb", 1, "Attack on Titan", domain="movie",
                 alt_titles=["Shingeki no Kyojin"])
    assert f.covers(film)


def test_an_unrelated_title_is_not_covered():
    f = Franchise(titles={("death", "note")})
    assert not f.covers(other("tmdb", 3, "Death Parade", domain="tv"))
    assert not f.covers(other("tmdb", 4, "Notes on a Scandal", domain="movie"))


def test_a_franchise_with_no_titles_matches_nothing_by_title():
    assert not Franchise().covers(other("tmdb", 1, "Anything", domain="movie"))


def test_names_come_from_the_work_asked_about_not_from_its_relatives(conn):
    """*Golden Kamuy*'s franchise includes an episode called *Monster*. Taking
    names from the whole franchise hid the unrelated *MONSTER* manga."""
    load(conn, [
        anilist(1, "Golden Kamuy", edges=[edge(2, "SIDE_STORY")]),
        anilist(2, "Monster"),
        anilist(30, "MONSTER", domain="manga"),
    ])
    f = franchise_of(conn, [uid(1)])
    assert uid(2) in f.uids                       # its relative, by edge
    assert ("monster",) not in f.titles           # but not by name
    assert not f.covers(anilist(30, "MONSTER", domain="manga"))


def test_names_come_from_every_row_of_the_work(conn):
    """The AniList row has the romaji title and the dump row the English one."""
    load(conn, [
        UnifiedItem(domain="anime", source="anilist", source_id="1",
                    title="Shingeki no Kyojin", description="Titans.",
                    external_ids={"mal_id": "1"}, fetched_at=utcnow()),
        UnifiedItem(domain="anime", source="mal", source_id="1",
                    title="Attack on Titan", external_ids={"mal_id": "1"},
                    fetched_at=utcnow()),
    ])
    f = franchise_of(conn, ["anime:anilist:1"])
    assert {("shingeki", "no", "kyojin"), ("attack", "on", "titan")} <= f.titles


def test_titles_too_short_to_mean_anything_are_not_used(conn):
    load(conn, [anilist(1, "K", alt_titles=["X", "蟲師"])])
    f = franchise_of(conn, [uid(1)])
    assert ("k",) not in f.titles and ("x",) not in f.titles
    assert ("蟲師",) in f.titles


# ---- through the API -----------------------------------------------------

#: 1 is rated. 2-4 are more of it that no relation edge reaches: a film and a
#: book from sources that assert no relations. 5-7 are genuinely something else,
#: and sit *behind* the franchise in the ranking -- which is the whole failure.
SEED = [
    anilist(1, "Death Note"),
    other("tmdb", 2, "Death Note", domain="movie"),
    other("tmdb", 3, "Death Note: The Last Name", domain="movie"),
    other("openlibrary", 4, "Death Note, Vol. 1", domain="book"),
    anilist(5, "Monster"),
    anilist(6, "Erased"),
    other("tmdb", 7, "Zodiac", domain="movie"),
]

VECTORS = {
    "anime:anilist:1": [1.0, 0.0],
    "movie:tmdb:2": [0.99, 0.10],
    "movie:tmdb:3": [0.98, 0.15],
    "book:openlibrary:4": [0.97, 0.20],
    "anime:anilist:5": [0.80, 0.60],
    "anime:anilist:6": [0.70, 0.70],
    "movie:tmdb:7": [0.60, 0.80],
}


@contextmanager
def serving(seed, vectors):
    """The app over ``seed``, with anime 1 rated and ``vectors`` as the model."""
    _load_similarity_index.cache_clear()
    _load_collaborative_index.cache_clear()
    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings.db_path)
    c = connect(settings.db_path)
    load(c, seed)
    set_rating(c, "anime:anilist:1", 9.0)
    c.close()
    write_matrix(settings.embeddings_path, vectors)
    from recsys.api.server import create_app

    with TestClient(create_app()) as tc:
        yield tc
    _load_similarity_index.cache_clear()
    _load_collaborative_index.cache_clear()


@pytest.fixture
def client():
    with serving(SEED, VECTORS) as tc:
        yield tc


def uids_of(body):
    return [n["item"]["item_uid"] for n in body["neighbours"]]


def test_recommendations_hold_back_what_only_a_title_connects(client):
    body = client.get("/api/recommend").json()
    assert uids_of(body) == ["anime:anilist:5", "anime:anilist:6", "movie:tmdb:7"]
    assert body["n_suppressed"] == 3


def test_the_page_is_filled_from_behind_the_franchise(client):
    """Asking for two must give two. The three nearest are all held back, so a
    fixed over-fetch of k would return nothing at all."""
    body = client.get("/api/recommend?k=2").json()
    assert uids_of(body) == ["anime:anilist:5", "anime:anilist:6"]


def test_the_franchise_comes_back_when_asked_for(client):
    body = client.get("/api/recommend?same_franchise=true").json()
    assert uids_of(body)[:3] == ["movie:tmdb:2", "movie:tmdb:3", "book:openlibrary:4"]
    assert body["n_suppressed"] == 0


def test_similar_holds_them_back_too(client):
    body = client.get("/api/similar/anime:anilist:1?k=2").json()
    assert uids_of(body) == ["anime:anilist:5", "anime:anilist:6"]
    assert body["n_suppressed"] == 3


def test_asking_for_one_domain_still_skips_the_franchise_in_it(client):
    body = client.get("/api/recommend?domain=movie").json()
    assert uids_of(body) == ["movie:tmdb:7"]


def test_several_domains_can_be_recommended_in_at_once(client):
    body = client.get("/api/recommend?domain=movie&domain=anime&same_franchise=true").json()
    assert {u.split(":")[0] for u in uids_of(body)} == {"movie", "anime"}
    assert "book:openlibrary:4" not in uids_of(body)
    assert body["domain"] is None                      # more than one, so none named
    assert client.get("/api/recommend?domain=movie").json()["domain"] == "movie"


def on_the_arc(rank):
    """A unit vector a little further from [1, 0] for each step of ``rank``."""
    return [math.cos(rank * 0.01), math.sin(rank * 0.01)]


def test_the_search_widens_until_the_page_is_full():
    """A long-running series has more entries than any fixed over-fetch. Forty
    films stand between the rating and the first new thing; the first fetch for
    k=1 is 22 deep and comes back all franchise, so only widening finds it."""
    films = [other("tmdb", 100 + n, f"Death Note: Part {n}", domain="movie")
             for n in range(40)]
    seed = [anilist(1, "Death Note"), *films, anilist(5, "Monster")]
    vectors = {"anime:anilist:1": on_the_arc(0)}
    vectors.update({film.item_uid: on_the_arc(n + 1) for n, film in enumerate(films)})
    vectors["anime:anilist:5"] = on_the_arc(60)

    with serving(seed, vectors) as tc:
        body = tc.get("/api/recommend?k=1").json()
    assert uids_of(body) == ["anime:anilist:5"]
    assert body["n_suppressed"] == 40


def test_a_neighbour_the_catalog_no_longer_has_is_passed_over():
    """The matrix is a snapshot. A uid in it that has since been pruned is not
    a recommendation and not an error -- and must not cost the page a slot."""
    vectors = {**VECTORS, "anime:anilist:999": on_the_arc(0.5)}   # nearest, and gone
    with serving(SEED, vectors) as tc:
        body = tc.get("/api/recommend?same_franchise=true&k=2").json()
    assert uids_of(body) == ["movie:tmdb:2", "movie:tmdb:3"]
