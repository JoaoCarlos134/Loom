"""Text embeddings: the bridge that does not need a shared vocabulary.

Loom's promise is that an anime and a novel can be compared. The obvious route
was matching tags, and it was measured and found badly asymmetric: for a typical
anime, 75% of its tags exist somewhere in the film vocabulary, but for a typical
book only **8%** of its tags exist in the anime vocabulary. The cause is shape,
not content -- AniList uses a curated 441-tag set while Goodreads and TMDB are
free-form with 27,067 and 18,643 tags, so anime tags are general enough to
appear everywhere and book tags are specific enough to appear nowhere else.

Embedding sidesteps that entirely. ``dystopia``, ``dystopian`` and "a
totalitarian future" land in the same region of the space without anyone
authoring a mapping between them, and the same model reads a synopsis and a
book blurb. There is no vocabulary to align because there is no vocabulary.

**The document is the whole work, not one row.** A book's prose lives on its
Open Library row while its shelves live on the goodbooks row; an anime's
synopsis is on the AniList row while the 2018 dump holds nothing. So the text
for a work is assembled from every row describing it -- title, the union of
their tags, and the longest description any of them carries. That also gives
items with no prose at all (music, and 23% of books) a vector built from title
and tags, so nothing is excluded from the space for lacking a synopsis.

Vectors are L2-normalised on the way out, so cosine similarity is a dot product
and `similarity.py` can compare against the whole catalog with one matmul.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from recsys.log import get_logger

log = get_logger(__name__)

#: How many characters of description to keep. These models truncate at a few
#: hundred tokens anyway, and a synopsis says what it is about in its opening.
MAX_DESCRIPTION = 900

#: An embedding backend: takes documents, yields one vector each.
Embedder = Callable[[Sequence[str]], Iterable[Sequence[float]]]


@dataclass
class EmbedResult:
    n_documents: int = 0
    n_skipped: int = 0
    dim: int = 0
    by_domain: dict[str, int] = field(default_factory=dict)


def compose_document(title: str, tags: Sequence[str], description: str | None) -> str:
    """The text that stands for a work.

    Title first, then tags, then prose. The order matters less than the fact
    that all three are present: an item with no synopsis still gets a usable
    vector from its title and tags rather than being dropped out of the space,
    which is the difference between the music domain participating and not.
    """
    parts = [title.strip()]
    if tags:
        parts.append(", ".join(dict.fromkeys(t.strip() for t in tags if t.strip())))
    if description:
        text = " ".join(description.split())
        parts.append(text[:MAX_DESCRIPTION])
    return ". ".join(p for p in parts if p)


def work_documents(conn: sqlite3.Connection) -> Iterator[tuple[str, str, str]]:
    """Yield (item_uid, domain, document) for every visible work.

    Assembled across a work's rows: one query joining each representative to its
    siblings, rather than a query per work. The representative supplies the
    identity; the siblings contribute whatever text and tags they carry.
    """
    rows = conn.execute(
        """SELECT i.item_uid, i.domain, i.title, i.tags, i.description,
                  sib.tags AS sib_tags, sib.description AS sib_description
           FROM items i
           LEFT JOIN links l ON l.item_uid = i.item_uid
           LEFT JOIN links l2 ON l2.work_uid = l.work_uid
           LEFT JOIN items sib ON sib.item_uid = l2.item_uid
                              AND sib.item_uid != i.item_uid
           WHERE (l.item_uid IS NULL OR l.is_primary = 1)
           ORDER BY i.item_uid"""
    )

    current: str | None = None
    domain = title = ""
    tags: dict[str, None] = {}
    description = ""

    def finish() -> tuple[str, str, str] | None:
        if current is None:
            return None
        document = compose_document(title, list(tags), description or None)
        return (current, domain, document) if document else None

    for row in rows:
        if row["item_uid"] != current:
            done = finish()
            if done:
                yield done
            current, domain, title = row["item_uid"], row["domain"], row["title"] or ""
            tags, description = {}, ""

        for column in ("tags", "sib_tags"):
            try:
                for tag in json.loads(row[column] or "[]"):
                    tags.setdefault(tag, None)
            except (json.JSONDecodeError, TypeError):
                pass
        # The longest description wins: a one-line stub and a real synopsis are
        # both "present", and only one of them is worth embedding.
        for column in ("description", "sib_description"):
            candidate = row[column] or ""
            if len(candidate) > len(description):
                description = candidate

    done = finish()
    if done:
        yield done


def build_embeddings(
    conn: sqlite3.Connection,
    embedder: Embedder,
    *,
    out_path,
    batch_size: int = 256,
) -> EmbedResult:
    """Embed every visible work and write the vectors to parquet.

    Written to ``DATA_DIR``, never the repo, like everything else derived. The
    file is replaced wholesale rather than updated in place -- the catalog
    changes rarely and in bulk, and a partial matrix is worse than an old one.
    """
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    result = EmbedResult()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Written to a temporary name and moved into place at the end, so an
    # interrupted build never leaves a half-written matrix that later loads as
    # if it were complete.
    partial = out_path.with_suffix(out_path.suffix + ".part")
    writer = None

    batch_uids: list[str] = []
    batch_domains: list[str] = []
    batch_texts: list[str] = []

    def flush() -> None:
        """Embed and *write* one batch.

        Streamed to disk a batch at a time rather than accumulated. A full build
        is tens of minutes and holding every vector in memory until the end
        means a failure at minute fifty costs all of it -- the same reasoning
        that made ingest/base.py flush its buffer on the failure path.
        """
        nonlocal writer
        if not batch_texts:
            return
        produced = list(embedder(batch_texts))
        if len(produced) != len(batch_texts):
            raise RuntimeError(
                f"embedder returned {len(produced)} vectors for {len(batch_texts)} documents"
            )
        block = np.asarray(produced, dtype="float32")
        # Normalised here rather than at query time so cosine is a dot product
        # and every consumer gets the same convention for free.
        norms = np.linalg.norm(block, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        block = block / norms

        if writer is None:
            result.dim = int(block.shape[1])
            schema = pa.schema([
                ("item_uid", pa.string()),
                ("domain", pa.string()),
                ("vector", pa.list_(pa.float32(), result.dim)),
            ])
            writer = pq.ParquetWriter(partial, schema, compression="zstd")

        writer.write_table(pa.table({
            "item_uid": pa.array(batch_uids, type=pa.string()),
            "domain": pa.array(batch_domains, type=pa.string()),
            "vector": pa.FixedSizeListArray.from_arrays(
                pa.array(block.reshape(-1), type=pa.float32()), result.dim
            ),
        }, schema=writer.schema))

        for d in batch_domains:
            result.by_domain[d] = result.by_domain.get(d, 0) + 1
        batch_uids.clear()
        batch_domains.clear()
        batch_texts.clear()

    try:
        for uid, domain, document in work_documents(conn):
            if not document.strip():
                result.n_skipped += 1
                continue
            batch_uids.append(uid)
            batch_domains.append(domain)
            batch_texts.append(document)
            result.n_documents += 1
            if len(batch_texts) >= batch_size:
                flush()
                if result.n_documents % (batch_size * 10) == 0:
                    log.info("embedded %d works", result.n_documents)
        flush()
    finally:
        if writer is not None:
            writer.close()

    if writer is None:
        log.warning("no documents to embed")
        partial.unlink(missing_ok=True)
        return result

    partial.replace(out_path)
    log.info("wrote %d x %d vectors to %s", result.n_documents, result.dim, out_path)
    return result
