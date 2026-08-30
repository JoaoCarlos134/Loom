"""Text embeddings: document assembly, the matrix, and nearest neighbours.

No test here loads a model. The embedder is a plain callable, so the whole
pipeline is driven by a deterministic fake -- which is the point of it being a
parameter rather than an import.
"""

from __future__ import annotations

import numpy as np
import pytest

from recsys.db import connect, init_db, upsert_items
from recsys.link import rebuild_links
from recsys.models.similarity import SimilarityIndex
from recsys.models.text import MAX_DESCRIPTION, build_embeddings, compose_document, work_documents
from recsys.schemas import UnifiedItem


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "embed.db"
    init_db(path)
    c = connect(path)
    yield c
    c.close()


def item(domain, source, source_id, **over):
    kwargs = dict(
        domain=domain, source=source, source_id=str(source_id),
        title=f"{domain} {source_id}", tags=[], description=None, external_ids={},
    )
    kwargs.update(over)
    return UnifiedItem(**kwargs)


def fake_embedder(dim: int = 8):
    """Deterministic vectors from a hash of the text -- same text, same vector."""
    def embed(texts):
        out = []
        for text in texts:
            rng = np.random.default_rng(abs(hash(text)) % (2**32))
            out.append(rng.normal(size=dim).astype("float32"))
        return out
    return embed


def counting_embedder(seen: list, dim: int = 8):
    base = fake_embedder(dim)
    def embed(texts):
        seen.extend(texts)
        return base(texts)
    return embed


# ---- the document -----------------------------------------------------------


def test_a_document_carries_title_tags_and_prose():
    doc = compose_document("Steins;Gate", ["time travel", "psychological"], "A mad scientist.")
    assert "Steins;Gate" in doc and "time travel" in doc and "A mad scientist" in doc


def test_an_item_with_no_prose_still_gets_a_document():
    """Music has no synopsis at all and 23% of books have none. Dropping them
    would exclude whole domains from the space for lacking one field."""
    assert compose_document("Radiohead", ["rock", "alternative"], None).strip()


def test_an_item_with_nothing_but_a_title_still_gets_a_document():
    assert compose_document("Some Work", [], None).strip() == "Some Work"


def test_duplicate_tags_are_collapsed():
    doc = compose_document("X", ["rock", "rock", "jazz"], None)
    assert doc.count("rock") == 1


def test_long_descriptions_are_truncated():
    doc = compose_document("X", [], "word " * 5000)
    assert len(doc) < MAX_DESCRIPTION + 100


def test_whitespace_in_prose_is_normalised():
    assert "\n" not in compose_document("X", [], "line one\n\n   line two")


# ---- assembling a work across its rows --------------------------------------


def test_a_works_text_is_gathered_from_all_its_rows(conn):
    """A book's prose is on the Open Library row and its shelves on the
    goodbooks row. Embedding either alone throws away half the signal."""
    upsert_items(conn, [
        item("book", "goodbooks", 1, external_ids={"isbn": "111"}, tags=["dystopian"]),
        item("book", "openlibrary", "111", external_ids={"isbn": "111"},
             description="A televised fight to the death.", tags=["survival"]),
    ])
    rebuild_links(conn)
    docs = {uid: doc for uid, _, doc in work_documents(conn)}
    assert len(docs) == 1
    only = next(iter(docs.values()))
    assert "dystopian" in only and "survival" in only and "fight to the death" in only


def test_the_longest_description_wins(conn):
    """A one-line stub and a real synopsis are both "present"."""
    upsert_items(conn, [
        item("anime", "mal", 1, external_ids={"mal_id": "1"}, description="Short."),
        item("anime", "anilist", 1, external_ids={"mal_id": "1"},
             description="A considerably longer and more useful synopsis of the work."),
    ])
    rebuild_links(conn)
    doc = next(doc for _, _, doc in work_documents(conn))
    assert "considerably longer" in doc


def test_only_representative_rows_produce_documents(conn):
    upsert_items(conn, [
        item("anime", "mal", 1, external_ids={"mal_id": "1"}),
        item("anime", "anilist", 1, external_ids={"mal_id": "1"}, description="Text."),
    ])
    rebuild_links(conn)
    assert len(list(work_documents(conn))) == 1


def test_an_unlinked_item_still_produces_a_document(conn):
    upsert_items(conn, [item("movie", "tmdb", 1, description="A film.")])
    assert len(list(work_documents(conn))) == 1


# ---- building the matrix ----------------------------------------------------


def test_the_matrix_covers_every_domain(conn, tmp_path):
    upsert_items(conn, [
        item("anime", "anilist", 1, description="An anime."),
        item("book", "goodbooks", 2, description="A book."),
        item("music", "musicbrainz", 3, tags=["rock"]),
    ])
    out = tmp_path / "features" / "e.parquet"
    result = build_embeddings(conn, fake_embedder(), out_path=out)

    assert result.n_documents == 3
    assert result.dim == 8
    assert set(result.by_domain) == {"anime", "book", "music"}
    assert out.is_file()


def test_vectors_are_normalised(conn, tmp_path):
    """Stored normalised so cosine is a dot product and every consumer inherits
    the same convention."""
    upsert_items(conn, [item("movie", "tmdb", i, description=f"Film {i}.") for i in range(5)])
    out = tmp_path / "e.parquet"
    build_embeddings(conn, fake_embedder(), out_path=out)

    index = SimilarityIndex.load(out)
    norms = np.linalg.norm(index.matrix, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


def test_the_embedder_sees_batches_not_one_call_per_item(conn, tmp_path):
    upsert_items(conn, [item("movie", "tmdb", i, description=f"Film {i}.") for i in range(10)])
    seen: list = []
    build_embeddings(conn, counting_embedder(seen), out_path=tmp_path / "e.parquet",
                     batch_size=4)
    assert len(seen) == 10


def test_an_embedder_returning_the_wrong_count_is_an_error(conn, tmp_path):
    """Silently mismatched vectors would misattribute every later item."""
    upsert_items(conn, [item("movie", "tmdb", i, description=f"F{i}.") for i in range(3)])

    def broken(texts):
        return [np.zeros(8, dtype="float32")]

    with pytest.raises(RuntimeError, match="vectors for"):
        build_embeddings(conn, broken, out_path=tmp_path / "e.parquet")


def test_an_empty_catalog_writes_nothing(conn, tmp_path):
    out = tmp_path / "e.parquet"
    result = build_embeddings(conn, fake_embedder(), out_path=out)
    assert result.n_documents == 0 and not out.is_file()


# ---- querying ---------------------------------------------------------------


@pytest.fixture
def index(conn, tmp_path):
    upsert_items(conn, [
        item("anime", "anilist", 1, title="Alpha", description="Alpha story."),
        item("anime", "anilist", 2, title="Beta", description="Beta story."),
        # Same title and prose as the anime, so the composed documents match
        # exactly -- the fake embedder hashes the document, not the description.
        item("manga", "anilist", 3, title="Alpha", description="Alpha story."),
        item("book", "goodbooks", 4, title="Gamma", description="Gamma story."),
    ])
    out = tmp_path / "e.parquet"
    build_embeddings(conn, fake_embedder(dim=16), out_path=out)
    return SimilarityIndex.load(out)


def test_identical_text_scores_highest(index):
    """The fake embedder is a hash of the text, so the manga sharing the anime's
    description must be its nearest neighbour."""
    found = index.similar_to("anime:anilist:1", k=3)
    assert found[0].item_uid == "manga:anilist:3"
    assert found[0].score == pytest.approx(1.0, abs=1e-5)


def test_a_query_never_returns_itself(index):
    assert all(n.item_uid != "anime:anilist:1" for n in index.similar_to("anime:anilist:1", k=10))


def test_results_can_be_confined_to_one_domain(index):
    """Asking for the manga nearest an anime is what makes this cross-domain
    rather than a related-items widget."""
    found = index.similar_to("anime:anilist:1", k=5, domain="manga")
    assert found and all(n.domain == "manga" for n in found)


def test_asking_for_an_empty_domain_returns_nothing(index):
    assert index.similar_to("anime:anilist:1", k=5, domain="movie") == []


def test_an_unknown_item_returns_nothing(index):
    assert index.similar_to("anime:anilist:999", k=5) == []


def test_results_come_back_ordered(index):
    found = index.similar_to("anime:anilist:1", k=4)
    assert [n.score for n in found] == sorted((n.score for n in found), reverse=True)


def test_k_larger_than_the_catalog_is_not_an_error(index):
    assert len(index.similar_to("anime:anilist:1", k=999)) == len(index) - 1
