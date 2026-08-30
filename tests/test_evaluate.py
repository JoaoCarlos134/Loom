"""Scoring the content model against links the catalog already asserts.

The evaluation matters more than it looks. Cross-domain similarity cannot be
judged by eye -- "is this novel a good match for this anime" has no obvious
answer -- so a model returning plausible nonsense is indistinguishable from one
that works until something measures it.

The title split is the part that keeps it honest: 87% of adaptation pairs share
a title and the embedded document leads with the title, so the headline recall
is substantially a string match congratulating itself.
"""

from __future__ import annotations

import numpy as np
import pytest

from recsys.db import connect, init_db, upsert_items
from recsys.models.evaluate import _same_title, adaptation_pairs, adaptation_recall
from recsys.models.text import build_embeddings
from recsys.schemas import UnifiedItem, utcnow


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "eval.db"
    init_db(path)
    c = connect(path)
    yield c
    c.close()


def anime(source_id, title, *, manga_id=None, relation="ADAPTATION", node_type="MANGA",
          description="An anime."):
    edges = []
    if manga_id is not None:
        edges = [{"relationType": relation, "node": {"id": manga_id, "type": node_type}}]
    return UnifiedItem(
        domain="anime", source="anilist", source_id=str(source_id), title=title,
        description=description, fetched_at=utcnow(),
        raw_json={"id": source_id, "relations": {"edges": edges}},
    )


def manga(source_id, title, description="A manga."):
    return UnifiedItem(
        domain="manga", source="anilist", source_id=str(source_id), title=title,
        description=description, fetched_at=utcnow(), raw_json={"id": source_id},
    )


def text_embedder(dim=16):
    def embed(texts):
        return [
            np.random.default_rng(abs(hash(t)) % (2**32)).normal(size=dim).astype("float32")
            for t in texts
        ]
    return embed


# ---- pairing --------------------------------------------------------------


def test_an_adaptation_pair_is_found(conn):
    upsert_items(conn, [anime(1, "Show", manga_id=50), manga(50, "Book")])
    assert adaptation_pairs(conn) == [("anime:anilist:1", "manga:anilist:50")]


def test_a_pair_whose_manga_is_absent_is_not_counted(conn):
    """Scoring against a target the index cannot contain would only ever fail."""
    upsert_items(conn, [anime(1, "Show", manga_id=999)])
    assert adaptation_pairs(conn) == []


@pytest.mark.parametrize("relation", ["CHARACTER", "SPIN_OFF", "OTHER", "SEQUEL"])
def test_loose_relations_are_not_ground_truth(conn, relation):
    """Only ADAPTATION and SOURCE mean "the same story in two media"."""
    upsert_items(conn, [anime(1, "Show", manga_id=50, relation=relation), manga(50, "Book")])
    assert adaptation_pairs(conn) == []


def test_a_relation_to_another_anime_is_not_a_pair(conn):
    upsert_items(conn, [anime(1, "Show", manga_id=50, node_type="ANIME"), manga(50, "Book")])
    assert adaptation_pairs(conn) == []


# ---- the title split ------------------------------------------------------


@pytest.mark.parametrize(
    "a,b",
    [("Steins;Gate", "Steins Gate"), ("Berserk", "berserk"),
     ("Steins;Gate", "Steins;Gate 0"), ("One Piece", "ONE PIECE!!")],
)
def test_titles_that_are_really_the_same_are_recognised(a, b):
    assert _same_title(a, b)


@pytest.mark.parametrize(
    "a,b",
    [("Cowboy Bebop", "Attack on Titan"), ("Spirited Away", "Berserk"), ("X", None), (None, None)],
)
def test_genuinely_different_titles_are_recognised(a, b):
    assert not _same_title(a, b)


def test_the_hard_subset_excludes_shared_title_pairs(conn, tmp_path):
    upsert_items(conn, [
        anime(1, "Same Title", manga_id=50), manga(50, "Same Title"),
        anime(2, "An Anime", manga_id=51), manga(51, "A Quite Different Manga"),
    ])
    out = tmp_path / "e.parquet"
    build_embeddings(conn, text_embedder(), out_path=out)

    result = adaptation_recall(conn, out, cutoffs=(1, 10))
    assert result.n_pairs == 2
    assert result.n_hard == 1  # only the differently-titled pair


def test_recall_is_perfect_when_the_texts_match(conn, tmp_path):
    """The fake embedder hashes the document, so identical documents give
    identical vectors and the true target must rank first."""
    upsert_items(conn, [
        anime(1, "Shared", manga_id=50, description="Exactly the same prose."),
        manga(50, "Shared", description="Exactly the same prose."),
        manga(51, "Unrelated", description="Something else entirely."),
    ])
    out = tmp_path / "e.parquet"
    build_embeddings(conn, text_embedder(), out_path=out)

    result = adaptation_recall(conn, out, cutoffs=(1,))
    assert result.n_pairs == 1
    assert result.recall(1) == 1.0


def test_the_baseline_scales_with_the_haystack(conn, tmp_path):
    upsert_items(conn, [anime(1, "A", manga_id=50)] + [manga(50 + i, f"M{i}") for i in range(20)])
    out = tmp_path / "e.parquet"
    build_embeddings(conn, text_embedder(), out_path=out)
    result = adaptation_recall(conn, out)
    assert result.n_manga == 20
    assert result.random_baseline == pytest.approx(0.5)


def test_no_pairs_is_reported_rather_than_divided_by(conn, tmp_path):
    upsert_items(conn, [anime(1, "Lonely")])
    out = tmp_path / "e.parquet"
    build_embeddings(conn, text_embedder(), out_path=out)
    result = adaptation_recall(conn, out)
    assert result.n_pairs == 0
    assert result.recall(10) == 0.0 and result.hard_recall(10) == 0.0
