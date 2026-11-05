"""Adaptations, franchises and book series: one card per story, not per entry.

Three versions of one failure, measured on a full build:

* rating the *PLUTO* manga still recommended the *PLUTO* anime -- suppression
  only knew franchise edges (sequels, side stories) and not adaptation ones;
* *BLAME!*'s manga and anime both filled page one of the same results;
* goodbooks-10k files every volume as its own book, so *Death Note* was eight
  cards of one series.

The fix has two halves. ``relations`` gains ``SERIES`` edges, read from the
"(Series, #n)" suffix Goodreads puts on a volume's title, and ``works`` stores
each work's group -- the connected component of its adaptation, franchise and
series edges, in representative space. Results suppress every work in a rated
or queried work's group and keep one work per group; listings show one card
per book series.
"""

from __future__ import annotations

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from recsys.api.routes import _load_collaborative_index, _load_similarity_index
from recsys.config import get_settings
from recsys.db import connect, count_items, init_db, query_items, upsert_items
from recsys.link import rebuild_links
from recsys.popularity import rebuild_popularity
from recsys.relations import rebuild_relations, series_name
from recsys.schemas import Creator, UnifiedItem
from recsys.works import rebuild_works


def anilist(source_id, title, *, domain="anime", edges=(), popularity=None):
    return UnifiedItem(
        domain=domain, source="anilist", source_id=str(source_id), title=title,
        popularity=popularity,
        raw_json={"id": source_id, "relations": {"edges": list(edges)}},
    )


def edge(node_id, relation, node_type="ANIME"):
    return {"relationType": relation, "node": {"id": node_id, "type": node_type}}


def book(source_id, title, *, author="Tsugumi Ohba", popularity=None, description=None):
    return UnifiedItem(
        domain="book", source="goodbooks", source_id=str(source_id), title=title,
        creators=[Creator(name=author, role="author")], popularity=popularity,
        description=description,
    )


DEATH_NOTE = [
    book(13615, "Death Note, Vol. 1: Boredom (Death Note, #1)", popularity=900),
    book(13619, "Death Note, Vol. 2: Confluence (Death Note, #2)", popularity=500),
    book(13618, "Death Note, Vol. 3: Hard Run (Death Note, #3)", popularity=400),
    book(13616, "Death Note, Vol. 4: Love (Death Note, #4)", popularity=350),
    book(13617, "Death Note, Vol. 5: Whiteout (Death Note, #5)", popularity=300),
    book(13620, "Death Note, Vol. 6: Give-and-Take (Death Note, #6)", popularity=280),
    book(13621, "Death Note, Vol. 7: Zero (Death Note, #7)", popularity=260),
    book(13614, "Death Note, Vol. 8: Target (Death Note, #8)", popularity=250),
]


def rebuild(conn):
    rebuild_links(conn)
    rebuild_popularity(conn)
    rebuild_relations(conn)
    rebuild_works(conn)


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "groups.db"
    init_db(path)
    c = connect(path)
    yield c
    c.close()


# ---- reading a series off a title -----------------------------------------


@pytest.mark.parametrize(("title", "expected"), [
    ("Death Note, Vol. 1: Boredom (Death Note, #1)", "Death Note"),
    ("The Hunger Games (The Hunger Games, #1)", "The Hunger Games"),
    ("A Storm of Swords: Blood and Gold (A Song of Ice and Fire, #3: Part 2 of 2)",
     "A Song of Ice and Fire"),
    ("Four: A Divergent Story Collection (Divergent, #0.1 - 0.4)", "Divergent"),
    ("The Twilight Saga Complete Collection  (Twilight, #1-4 + 3.5)", "Twilight"),
    ("Foundation (Foundation #1)", "Foundation"),
    ("The Sandman, Vol. 1: Preludes and Nocturnes (The Sandman #1)", "The Sandman"),
    ("The Walking Dead, Vol. 01: Days Gone Bye", "The Walking Dead"),
    ("Scott Pilgrim, Volume 1: Scott Pilgrim's Precious Little Life", "Scott Pilgrim"),
    ("Reaper Man (Discworld, #11; Death, #2)", "Discworld"),
    ("Pride and Prejudice", None),
    # No number means no claim to be a volume: Goodreads uses the same
    # parentheses for imprints ("A Little Golden Book") and subtitles.
    ("The Poky Little Puppy (A Little Golden Book)", None),
    ("Sleeping Beauty (Disney Princess, 5)", None),
])
def test_a_series_is_read_from_the_goodreads_title_suffix(title, expected):
    assert series_name(title) == expected


@pytest.mark.parametrize(("title", "collection"), [
    ("The Foundation Trilogy (Foundation, #1-3)", True),
    ("The Oedipus Cycle (The Theban Plays, #1\u20133)", True),
    ("The Twilight Saga Complete Collection  (Twilight, #1-4 + 3.5)", True),
    ("Reaper Man (Discworld, #11; Death, #2)", False),
    # A comma, then a second series' name: two memberships, not a boxed set.
    ("Raising Steam (Discworld, #40, Moist von Lipwig #3 )", False),
    ("The Lost Years (Merlin, #1, 2 & 3)", True),
    ("A Storm of Swords: Blood and Gold (A Song of Ice and Fire, #3: Part 2 of 2)", False),
])
def test_a_boxed_set_is_told_apart_from_a_volume(title, collection):
    from recsys.relations import series_of

    assert series_of(title).is_collection is collection


# ---- listings: one card per series ----------------------------------------


def test_volumes_of_one_series_are_one_card(conn):
    upsert_items(conn, [*DEATH_NOTE, book(5470, "1984", author="George Orwell", popularity=50)])
    rebuild(conn)
    total, items = query_items(conn, domain="book")
    assert total == 2
    assert [i.title for i in items] == [
        "Death Note, Vol. 1: Boredom (Death Note, #1)", "1984",
    ]
    assert count_items(conn) == 2


def test_the_series_card_is_its_most_read_volume(conn):
    """Not the lowest number: a series is shown by the volume people know."""
    upsert_items(conn, [
        book(1, "Thing, Vol. 1 (Thing, #1)", popularity=10),
        book(2, "Thing, Vol. 2 (Thing, #2)", popularity=99),
    ])
    rebuild(conn)
    assert [i.source_id for i in query_items(conn)[1]] == ["2"]


def test_search_shows_the_volume_that_matched_once_per_series(conn):
    """Vol. 1 -- the series' card -- also mentions a whiteout in its blurb, so
    both match; the volume whose *title* matches is the one shown."""
    first = DEATH_NOTE[0].model_copy(update={"description": "Before the whiteout."})
    upsert_items(conn, [first, *DEATH_NOTE[1:]])
    rebuild(conn)
    total, items = query_items(conn, q="Whiteout")
    assert [i.source_id for i in items] == ["13617"] and total == 1
    total, items = query_items(conn, q="Death Note")
    assert total == 1 and len(items) == 1


def test_a_boxed_set_is_never_the_series_card(conn):
    upsert_items(conn, [
        book(1, "The Thing Trilogy (Thing, #1-3)", popularity=999),
        book(2, "Thing (Thing, #1)", popularity=500),
        book(3, "Thing Two (Thing, #2)", popularity=400),
    ])
    rebuild(conn)
    assert [i.source_id for i in query_items(conn)[1]] == ["2"]


def test_folding_can_be_turned_off_and_the_series_still_groups_results(conn):
    upsert_items(conn, DEATH_NOTE[:3])
    rebuild_links(conn)
    rebuild_popularity(conn)
    rebuild_relations(conn)
    rebuild_works(conn, fold_series=False)
    assert query_items(conn, domain="book")[0] == 3
    groups = {r[0] for r in conn.execute("SELECT group_uid FROM works")}
    assert len(groups) == 1 and None not in groups


def test_a_series_whose_volumes_are_represented_by_open_library_rows(conn):
    """The SERIES edges sit on the Goodreads rows; Open Library rows win the
    primary election on text and carry no "(Death Note, #n)" suffix. The
    series has to be found in representative space all the same."""
    from recsys.db import series_members

    rows = []
    for n, vol in enumerate(DEATH_NOTE[:3], start=1):
        isbn = f"142150{n:04d}"
        rows.append(vol.model_copy(update={"external_ids": {"isbn": isbn}}))
        rows.append(UnifiedItem(
            domain="book", source="openlibrary", source_id=isbn,
            title=f"Death Note {n}", description=f"Volume {n} of the story.",
            external_ids={"isbn": isbn},
        ))
    upsert_items(conn, rows)
    rebuild(conn)

    total, items = query_items(conn, domain="book")
    assert total == 1 and items[0].source == "openlibrary"
    name, volumes = series_members(conn, items[0].item_uid)
    assert name == "Death Note"
    assert [v.title for v in volumes] == ["Death Note 1", "Death Note 2", "Death Note 3"]


def test_same_series_name_by_two_authors_is_two_series(conn):
    upsert_items(conn, [
        book(1, "Legacy (Legacy, #1)", author="A. Author", popularity=5),
        book(2, "Legacy Two (Legacy, #2)", author="A. Author", popularity=4),
        book(3, "Legacy (Legacy, #1)", author="Someone Else", popularity=3),
    ])
    rebuild(conn)
    assert query_items(conn)[0] == 2


def test_a_volume_still_opens_by_uid(conn):
    """Collapsed from listings, never from the catalog."""
    from recsys.db import get_item

    upsert_items(conn, DEATH_NOTE)
    rebuild(conn)
    assert get_item(conn, "book:goodbooks:13617").title.startswith("Death Note, Vol. 5")


# ---- results: suppression and collapse ------------------------------------


def write_matrix(path, vectors):
    uids = list(vectors)
    block = np.asarray([vectors[u] for u in uids], dtype="float32")
    block /= np.linalg.norm(block, axis=1, keepdims=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({
        "item_uid": pa.array(uids, type=pa.string()),
        "domain": pa.array([u.split(":")[0] for u in uids], type=pa.string()),
        "vector": pa.FixedSizeListArray.from_arrays(
            pa.array(block.reshape(-1), type=pa.float32()), block.shape[1]
        ),
    }), path)


#: 10 is the PLUTO manga; 11 its anime, which says SOURCE -> 10 and nothing
#: more. 20 is the BLAME! manga, 21 its film; the manga carries the ADAPTATION
#: edge, so the film's own row knows nothing of it. 30 -> 31 -> 32 are seasons
#: chained by SEQUEL, so 32 is two hops from 30. 40 and 41 are unrelated.
SEED = [
    anilist(10, "PLUTO", domain="manga"),
    anilist(11, "PLUTO", edges=[edge(10, "SOURCE", "MANGA")]),
    anilist(20, "BLAME!", domain="manga", edges=[edge(21, "ADAPTATION")]),
    anilist(21, "BLAME! (film)"),
    anilist(30, "Season 1", edges=[edge(31, "SEQUEL")]),
    anilist(31, "Season 2", edges=[edge(32, "SEQUEL")]),
    anilist(32, "Season 3"),
    anilist(40, "Monster"),
    anilist(41, "Ergo Proxy"),
    *DEATH_NOTE[:3],
]

VECTORS = {
    "manga:anilist:10": [1.0, 0.0, 0.0],
    "anime:anilist:11": [0.99, 0.05, 0.0],
    "manga:anilist:20": [0.9, 0.3, 0.0],
    "anime:anilist:21": [0.88, 0.32, 0.0],
    "anime:anilist:30": [0.0, 1.0, 0.0],
    "anime:anilist:31": [0.0, 0.99, 0.1],
    "anime:anilist:32": [0.0, 0.98, 0.15],
    "anime:anilist:40": [0.7, 0.5, 0.1],
    "anime:anilist:41": [0.6, 0.6, 0.2],
    "book:goodbooks:13615": [0.95, 0.1, 0.1],
    "book:goodbooks:13619": [0.94, 0.12, 0.1],
    "book:goodbooks:13618": [0.93, 0.13, 0.1],
}


@pytest.fixture
def client():
    _load_similarity_index.cache_clear()
    _load_collaborative_index.cache_clear()
    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings.db_path)
    c = connect(settings.db_path)
    upsert_items(c, SEED)
    rebuild(c)
    c.close()
    write_matrix(settings.embeddings_path, VECTORS)
    from recsys.api.server import create_app

    with TestClient(create_app()) as tc:
        yield tc
    _load_similarity_index.cache_clear()


def uids(body):
    return [n["item"]["item_uid"] for n in body["neighbours"]]


def test_rating_a_manga_never_recommends_its_own_anime(client):
    """The PLUTO case: the anime names the manga as its SOURCE, one way only."""
    client.put("/api/ratings/manga:anilist:10", json={"rating": 9})
    assert "anime:anilist:11" not in uids(client.get("/api/recommend").json())


def test_an_adaptation_edge_counts_from_the_side_that_lacks_it(client):
    """The BLAME! film's own row has no edge; the manga's does."""
    client.put("/api/ratings/anime:anilist:21", json={"rating": 9})
    assert "manga:anilist:20" not in uids(client.get("/api/recommend").json())


def test_a_rated_seasons_whole_franchise_is_suppressed(client):
    """Season 3 is two SEQUEL hops from season 1 and is still its franchise."""
    client.put("/api/ratings/anime:anilist:30", json={"rating": 9})
    got = uids(client.get("/api/recommend").json())
    assert "anime:anilist:31" not in got
    assert "anime:anilist:32" not in got


def test_results_keep_one_work_per_adaptation_group(client):
    """BLAME!'s manga and film are both near Monster; only the nearer shows --
    the film, by the fixture's vectors."""
    body = client.get("/api/similar/anime:anilist:40?k=20").json()
    got = uids(body)
    assert "anime:anilist:21" in got
    assert "manga:anilist:20" not in got
    assert body["n_collapsed"] >= 1


def test_results_keep_one_volume_per_series(client):
    got = uids(client.get("/api/similar/manga:anilist:10?k=20&domain=book").json())
    assert got == ["book:goodbooks:13615"]


def test_collapsing_does_not_shorten_the_page(client):
    body = client.get("/api/similar/anime:anilist:40?k=4").json()
    assert len(body["neighbours"]) == 4
    assert len(set(uids(body))) == 4


def test_the_franchise_can_still_be_asked_for(client):
    got = uids(client.get("/api/similar/anime:anilist:30?same_franchise=true").json())
    assert got[:2] == ["anime:anilist:31", "anime:anilist:32"]


def test_listings_say_how_many_volumes_a_card_stands_for(client):
    body = client.get("/api/items?domain=book").json()
    assert body["total"] == 1
    assert body["series_sizes"] == {"book:goodbooks:13615": 3}


def test_a_series_lists_its_volumes_in_order(client):
    body = client.get("/api/series/book:goodbooks:13618").json()
    assert [i["source_id"] for i in body["items"]] == ["13615", "13619", "13618"]
    assert body["series"] == "Death Note"


def test_a_work_in_no_series_has_none(client):
    assert client.get("/api/series/anime:anilist:40").status_code == 404


def test_a_component_too_big_to_be_one_story_is_left_ungrouped(conn):
    """A chain of crossovers can join franchises into one component. Past the
    cap it is not one story: no group, so nothing is suppressed wholesale --
    and the rebuild says so."""
    chain = [anilist(n, f"Entry {n}", edges=[edge(n + 1, "SIDE_STORY")]) for n in range(1, 6)]
    upsert_items(conn, [*chain, anilist(6, "Entry 6")])
    rebuild_links(conn)
    rebuild_popularity(conn)
    rebuild_relations(conn)
    result = rebuild_works(conn, max_group_size=4)
    assert result.oversized_groups == [(6, "anime:anilist:1")]
    assert conn.execute("SELECT COUNT(*) FROM works WHERE group_uid IS NOT NULL").fetchone()[0] == 0

    result = rebuild_works(conn, max_group_size=6)
    assert result.oversized_groups == []
    assert result.largest_groups == [(6, "anime:anilist:1")]



def test_by_default_one_hop_reaches_adaptations_and_series_too():
    """`franchise_representatives` used to know sequels only, which is why the
    PLUTO anime survived the PLUTO manga being rated."""
    from recsys.relations import GROUP_RELATIONS, franchise_representatives

    assert {"ADAPTATION", "SOURCE", "SERIES", "SEQUEL"} <= GROUP_RELATIONS
    assert franchise_representatives.__defaults__ == (GROUP_RELATIONS,)


def test_recommendations_keep_one_work_per_story(client):
    """Rated Ergo Proxy; BLAME!'s manga and film are both near it."""
    client.put("/api/ratings/anime:anilist:41", json={"rating": 9})
    body = client.get("/api/recommend?k=20").json()
    got = uids(body)
    assert len({"manga:anilist:20", "anime:anilist:21"} & set(got)) == 1
    assert len({"manga:anilist:10", "anime:anilist:11"} & set(got)) == 1
    assert body["n_collapsed"] >= 2


@pytest.fixture
def crowded():
    """Q, with a fourteen-entry sequel chain right beside it and six unrelated
    works further out. Collapsing the chain must not leave the page short."""
    _load_similarity_index.cache_clear()
    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings.db_path)
    c = connect(settings.db_path)
    chain = [anilist(100 + n, f"Gundam {n}", edges=[edge(101 + n, "SEQUEL")]) for n in range(13)]
    chain.append(anilist(113, "Gundam 13"))
    others = [anilist(200 + n, f"Other {n}") for n in range(6)]
    upsert_items(c, [anilist(1, "Q"), *chain, *others])
    rebuild(c)
    c.close()
    vectors = {"anime:anilist:1": [1.0, 0.0]}
    vectors.update({f"anime:anilist:{100 + n}": [1.0, 0.001 * (n + 1)] for n in range(14)})
    vectors.update({f"anime:anilist:{200 + n}": [0.5, 0.5 + 0.01 * n] for n in range(6)})
    write_matrix(settings.embeddings_path, vectors)
    from recsys.api.server import create_app

    with TestClient(create_app()) as tc:
        yield tc
    _load_similarity_index.cache_clear()


def test_a_big_group_beside_the_query_does_not_shorten_the_page(crowded):
    body = crowded.get("/api/similar/anime:anilist:1?k=3").json()
    got = uids(body)
    assert len(got) == 3
    assert sum(u.startswith("anime:anilist:1") and u != "anime:anilist:1" for u in got) == 1


def test_nor_does_it_shorten_recommendations(crowded):
    crowded.put("/api/ratings/anime:anilist:1", json={"rating": 9})
    assert len(uids(crowded.get("/api/recommend?k=3").json())) == 3
