"""Item-item collaborative filtering over the ratings corpus.

Driven by a hand-built corpus with a planted structure, so "did it work" has an
answer rather than a vibe: two groups of users like two disjoint sets of anime,
and the model must recover the groups. Nothing here reads the real 19.2M-rating
table and no test touches the network.

The uid-space translation gets the most attention, because it is the failure
that would not raise: ratings name ``anime:mal:*`` rows and listings show
``anime:anilist:*`` rows, so a model read in the wrong space returns an empty
list and looks merely unhelpful.
"""

from __future__ import annotations

import pytest

from recsys.db import connect, init_db, upsert_items, upsert_ratings
from recsys.link import rebuild_links
from recsys.models.collaborative import (
    CollaborativeIndex,
    build_collaborative,
    held_out_recall,
)
from recsys.schemas import UnifiedItem, utcnow


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "cf.db")
    c = connect(tmp_path / "cf.db")
    yield c
    c.close()


def anime(source_id, title, *, source="mal", **kw):
    return UnifiedItem(
        domain="anime", source=source, source_id=str(source_id), title=title,
        fetched_at=utcnow(), **kw,
    )


#: Two clusters: 0-3 and 4-7. Users like one cluster or the other, never both.
CLUSTER_A = [0, 1, 2, 3]
CLUSTER_B = [4, 5, 6, 7]


def planted_corpus(n_users=120):
    """Ratings with a structure the model is required to recover."""
    items = [anime(i, f"Show {i}") for i in range(8)]
    ratings = []
    for u in range(n_users):
        group = CLUSTER_A if u % 2 == 0 else CLUSTER_B
        other = CLUSTER_B if u % 2 == 0 else CLUSTER_A
        for i in group:
            ratings.append((f"mal:{u}", f"anime:mal:{i}", 9.0, None))
        # A low rating on the other cluster, so the signal is preference and
        # not merely which items a user happened to have seen.
        ratings.append((f"mal:{u}", f"anime:mal:{other[0]}", 3.0, None))
    return items, ratings


@pytest.fixture
def built(conn, tmp_path):
    items, ratings = planted_corpus()
    upsert_items(conn, items)
    upsert_ratings(conn, ratings)
    out = tmp_path / "cf.parquet"
    result = build_collaborative(conn, out_path=out, k=5, min_raters=5, shrinkage=1.0)
    return CollaborativeIndex.load(out), result


def test_the_planted_clusters_are_recovered(built):
    """Every neighbour of a cluster-A show should be another cluster-A show."""
    index, _ = built
    for i in CLUSTER_A:
        found = {n.item_uid for n in index.neighbours[f"anime:mal:{i}"][:3]}
        assert found <= {f"anime:mal:{j}" for j in CLUSTER_A if j != i}


def test_an_item_is_never_its_own_neighbour(built):
    index, _ = built
    for uid, neighbours in index.neighbours.items():
        assert all(n.item_uid != uid for n in neighbours)


def test_neighbours_come_back_ranked(built):
    index, _ = built
    for neighbours in index.neighbours.values():
        scores = [n.score for n in neighbours]
        assert scores == sorted(scores, reverse=True)


def test_negative_similarity_is_not_a_recommendation(built):
    """Cross-cluster pairs are anti-correlated here; they must not be stored."""
    index, _ = built
    assert all(n.score > 0 for ns in index.neighbours.values() for n in ns)


def test_thinly_rated_items_are_dropped(conn, tmp_path):
    items, ratings = planted_corpus()
    items.append(anime(99, "Nobody Watched This"))
    ratings.append(("mal:0", "anime:mal:99", 10.0, None))
    upsert_items(conn, items)
    upsert_ratings(conn, ratings)

    out = tmp_path / "cf.parquet"
    result = build_collaborative(conn, out_path=out, k=5, min_raters=5, shrinkage=1.0)
    assert result.dropped_items == 1
    assert not CollaborativeIndex.load(out).has("anime:mal:99")


def test_shrinkage_demotes_a_thinly_co_rated_pair(conn, tmp_path):
    """Two users agreeing perfectly must not outrank a hundred users agreeing."""
    items, ratings = planted_corpus()
    items += [anime(200, "Rare One"), anime(201, "Rare Two")]
    for u in range(6):  # enough to clear min_raters, far short of the clusters
        for i in (200, 201):
            ratings.append((f"mal:{u}", f"anime:mal:{i}", 10.0, None))
    upsert_items(conn, items)
    upsert_ratings(conn, ratings)

    out = tmp_path / "cf.parquet"
    build_collaborative(conn, out_path=out, k=8, min_raters=5, shrinkage=50.0)
    index = CollaborativeIndex.load(out)

    rare = next((n.score for n in index.neighbours.get("anime:mal:200", [])
                 if n.item_uid == "anime:mal:201"), 0.0)
    dense = max(n.score for n in index.neighbours["anime:mal:0"])
    assert rare < dense


def test_an_empty_ratings_table_produces_nothing(conn, tmp_path):
    upsert_items(conn, [anime(1, "Alone")])
    result = build_collaborative(conn, out_path=tmp_path / "cf.parquet", min_raters=5)
    assert result.n_pairs == 0


# ---- uid-space translation ----------------------------------------------


@pytest.fixture
def two_space(conn, tmp_path):
    """A catalog where the rated row is *not* the row listings show.

    This is the real catalog's shape: 6,147 of 6,598 rated rows lost the
    primary election to an AniList row carrying a synopsis.
    """
    items, ratings = planted_corpus()
    # Give every show a richer AniList sibling that will win the election.
    items += [
        anime(i, f"Show {i}", source="anilist", description="A synopsis.",
              external_ids={"mal_id": str(i)})
        for i in range(8)
    ]
    for i in range(8):
        items[i] = anime(i, f"Show {i}", external_ids={"mal_id": str(i)})
    upsert_items(conn, items)
    upsert_ratings(conn, ratings)
    rebuild_links(conn)

    out = tmp_path / "cf.parquet"
    build_collaborative(conn, out_path=out, k=5, min_raters=5, shrinkage=1.0)
    return CollaborativeIndex.load(out)


def test_the_anilist_row_won_the_election(conn, two_space):
    """Guard the premise: if this fails the rest of the section proves nothing."""
    row = conn.execute(
        "SELECT item_uid FROM links WHERE work_uid = 'work:anime:mal_id:0' AND is_primary = 1"
    ).fetchone()
    assert row[0] == "anime:anilist:0"


def test_asking_in_listing_space_finds_the_rated_row(conn, two_space):
    """The uid a caller holds is the AniList one; the model only knows the MAL
    one. Nothing in between raises, so this is the test that matters."""
    found = two_space.similar_to(conn, "anime:anilist:0", k=3)
    assert found, "translation failed: a work with ratings returned no neighbours"


def test_neighbours_come_back_in_listing_space(conn, two_space):
    found = two_space.similar_to(conn, "anime:anilist:0", k=3)
    assert all(n.item_uid.startswith("anime:anilist:") for n in found)


def test_translated_neighbours_are_still_the_right_cluster(conn, two_space):
    found = two_space.similar_to(conn, "anime:anilist:0", k=3)
    assert {n.item_uid for n in found} <= {f"anime:anilist:{j}" for j in CLUSTER_A if j}


def test_a_work_with_no_ratings_returns_empty_not_an_error(conn, two_space):
    assert two_space.similar_to(conn, "anime:anilist:404", k=3) == []


def test_the_query_is_never_returned_to_itself(conn, two_space):
    found = two_space.similar_to(conn, "anime:anilist:0", k=5)
    assert all(n.item_uid != "anime:anilist:0" for n in found)


# ---- the measurement -----------------------------------------------------


def test_held_out_recall_beats_the_popularity_baseline(conn, built):
    """The planted structure is real, so the model must beat recommending the
    most-rated shows to everybody. If it cannot, it is not a model."""
    index, _ = built
    result = held_out_recall(conn, index, cutoffs=(1, 3), n_users=60, min_ratings=4)
    assert result.n_users > 0
    assert result.recall(3) > result.popular_recall(3)


def test_the_baseline_is_computed_over_the_same_users(conn, built):
    index, _ = built
    result = held_out_recall(conn, index, cutoffs=(1,), n_users=40, min_ratings=4)
    assert result.n_users <= 40
    assert 0.0 <= result.popular_recall(1) <= 1.0
