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

**Each vector carries the fingerprint of the document it was made from**, and of
the model. A full build is 2.5 hours, and the only way to know whether one was
needed used to be knowing what had changed. Now a rebuild embeds only works
whose document moved or that are new, keeps every other vector, and
``matrix_freshness`` reports the difference without embedding anything. A
matrix from before fingerprints is kept and reported as unverified -- never
claimed fresh -- until a full rebuild replaces it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    n_embedded: int = 0  # documents sent to the embedder this run
    n_reused: int = 0  # vectors carried over from the previous matrix
    dim: int = 0
    by_domain: dict[str, int] = field(default_factory=dict)


def doc_fingerprint(document: str, model: str) -> str:
    """What a vector was made from: the document and the model, hashed.

    The model is part of it because a vector is only comparable with vectors
    from the same model -- switching models must re-embed everything.
    """
    return hashlib.sha256(f"{model}\0{document}".encode()).hexdigest()[:24]


@dataclass
class Freshness:
    """How far a matrix is from the catalog it was built for."""

    n_works: int = 0  # documents the catalog would embed today
    n_current: int = 0  # vectors whose fingerprint matches today's document
    n_changed: int = 0  # vectors whose document has changed since
    n_missing: int = 0  # works with no vector at all
    n_orphaned: int = 0  # vectors for works that no longer exist or represent
    n_unknown: int = 0  # vectors from before fingerprints: unverifiable

    @property
    def is_fresh(self) -> bool:
        return self.n_current == self.n_works and not (self.n_orphaned or self.n_unknown)

    @property
    def n_to_embed(self) -> int:
        """What an incremental rebuild would send to the embedder."""
        return self.n_changed + self.n_missing


def _fingerprints(path: Path) -> dict[str, str | None]:
    """item_uid -> fingerprint (None for a vector from before fingerprints).

    Reads two columns only: checking freshness never needs the vectors.
    """
    import pyarrow.parquet as pq

    if not Path(path).is_file():
        return {}
    names = pq.read_schema(path).names
    columns = ["item_uid", "doc_hash"] if "doc_hash" in names else ["item_uid"]
    table = pq.read_table(path, columns=columns)
    uids = table.column("item_uid").to_pylist()
    hashes = (table.column("doc_hash").to_pylist() if "doc_hash" in names
              else [None] * len(uids))
    return dict(zip(uids, hashes, strict=True))


def _previous(path: Path) -> dict[str, tuple[str | None, Any]]:
    """item_uid -> (fingerprint or None, vector) from an existing matrix.

    Vectors come back as rows of one numpy array, not Python lists: for the
    full build's 84k x 384 that is half a second instead of eleven, and a
    tenth of the memory.
    """
    import numpy as np
    import pyarrow.parquet as pq

    if not Path(path).is_file():
        return {}
    hashes = _fingerprints(path)
    table = pq.read_table(path, columns=["item_uid", "vector"])
    dim = table.schema.field("vector").type.list_size
    flat = table.column("vector").combine_chunks().flatten().to_numpy(zero_copy_only=False)
    matrix = np.asarray(flat, dtype="float32").reshape(-1, dim)
    uids = table.column("item_uid").to_pylist()
    return {uid: (hashes.get(uid), matrix[i]) for i, uid in enumerate(uids)}


def matrix_freshness(conn: sqlite3.Connection, path: Path, *, model: str) -> Freshness:
    """Compare a matrix with what the catalog would embed today. Embeds nothing."""
    previous = _fingerprints(path)
    result = Freshness()
    seen: set[str] = set()
    for uid, _, document in work_documents(conn):
        if not document.strip():
            continue
        result.n_works += 1
        seen.add(uid)
        old = previous.get(uid)
        if uid not in previous:
            result.n_missing += 1
        elif old is None:
            result.n_unknown += 1
        elif old == doc_fingerprint(document, model):
            result.n_current += 1
        else:
            result.n_changed += 1
    result.n_orphaned = sum(1 for uid in previous if uid not in seen)
    return result


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
    model: str = "",
    reuse: bool = True,
) -> EmbedResult:
    """Embed every visible work and write the vectors to parquet.

    Written to ``DATA_DIR``, never the repo, like everything else derived. The
    file is replaced wholesale -- a partial matrix is worse than an old one --
    but with ``reuse`` (the default) a vector whose fingerprint matches today's
    document is carried over rather than recomputed, and so is a vector from a
    matrix older than fingerprints, which stays marked unverified. Pass
    ``reuse=False`` to embed everything again.
    """
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    result = EmbedResult()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    previous = _previous(out_path) if reuse else {}
    # Written to a temporary name and moved into place at the end, so an
    # interrupted build never leaves a half-written matrix that later loads as
    # if it were complete.
    partial = out_path.with_suffix(out_path.suffix + ".part")
    writer = None

    def write(uids: list[str], domains: list[str], hashes: list[str | None], block) -> None:
        nonlocal writer
        if writer is not None and int(block.shape[1]) != result.dim:
            # Kept vectors from a matrix older than fingerprints carry no model
            # name, so a model change shows up here first.
            raise RuntimeError(
                f"the matrix on disk holds {result.dim}-dimension vectors and the "
                f"embedder makes {block.shape[1]}; rebuild it with scripts/embed.py --full"
            )
        if writer is None:
            result.dim = int(block.shape[1])
            schema = pa.schema([
                ("item_uid", pa.string()),
                ("domain", pa.string()),
                ("vector", pa.list_(pa.float32(), result.dim)),
                ("doc_hash", pa.string()),
            ])
            writer = pq.ParquetWriter(partial, schema, compression="zstd")
        writer.write_table(pa.table({
            "item_uid": pa.array(uids, type=pa.string()),
            "domain": pa.array(domains, type=pa.string()),
            "vector": pa.FixedSizeListArray.from_arrays(
                pa.array(block.reshape(-1), type=pa.float32()), result.dim
            ),
            "doc_hash": pa.array(hashes, type=pa.string()),
        }, schema=writer.schema))
        for d in domains:
            result.by_domain[d] = result.by_domain.get(d, 0) + 1

    pending: list[tuple[str, str, str, str]] = []  # uid, domain, document, fingerprint
    kept: list[tuple[str, str, str | None, Any]] = []  # uid, domain, fingerprint, vector

    def flush_pending() -> None:
        """Embed and *write* one batch.

        Streamed to disk a batch at a time rather than accumulated. A full build
        is hours, and holding every vector in memory until the end means a
        failure late on costs all of it -- the same reasoning that made
        ingest/base.py flush its buffer on the failure path.
        """
        if not pending:
            return
        texts = [p[2] for p in pending]
        produced = list(embedder(texts))
        if len(produced) != len(texts):
            raise RuntimeError(
                f"embedder returned {len(produced)} vectors for {len(texts)} documents"
            )
        block = np.asarray(produced, dtype="float32")
        # Normalised here rather than at query time so cosine is a dot product
        # and every consumer gets the same convention for free.
        norms = np.linalg.norm(block, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        write([p[0] for p in pending], [p[1] for p in pending],
              [p[3] for p in pending], block / norms)
        result.n_embedded += len(pending)
        pending.clear()

    def flush_kept() -> None:
        if not kept:
            return
        write([k[0] for k in kept], [k[1] for k in kept], [k[2] for k in kept],
              np.asarray([k[3] for k in kept], dtype="float32"))
        result.n_reused += len(kept)
        kept.clear()

    try:
        for uid, domain, document in work_documents(conn):
            if not document.strip():
                result.n_skipped += 1
                continue
            result.n_documents += 1
            fingerprint = doc_fingerprint(document, model)
            old = previous.get(uid)
            # Reused when it was made from exactly this document, or when it
            # predates fingerprints and so cannot be shown to be stale.
            if old is not None and old[0] in (fingerprint, None):
                kept.append((uid, domain, old[0], old[1]))
                if len(kept) >= 4096:
                    flush_kept()
                continue
            pending.append((uid, domain, document, fingerprint))
            if len(pending) >= batch_size:
                flush_pending()
                if result.n_embedded % (batch_size * 10) == 0:
                    log.info("embedded %d works", result.n_embedded)
        flush_kept()
        flush_pending()
    finally:
        if writer is not None:
            writer.close()

    if writer is None:
        log.warning("no documents to embed")
        partial.unlink(missing_ok=True)
        return result

    partial.replace(out_path)
    log.info("wrote %d x %d vectors to %s (%d embedded, %d reused)",
             result.n_documents, result.dim, out_path, result.n_embedded, result.n_reused)
    return result


def adopt_fingerprints(conn: sqlite3.Connection, path: Path, *, model: str) -> int:
    """Stamp today's fingerprints on vectors from before fingerprints. Returns how many.

    A claim, made deliberately: "these vectors were made from today's
    documents". Nothing can check it -- that is why such vectors are reported
    as unverified -- so it is never done implicitly. It is right when the
    matrix is known current (the full build's was verified so on 2026-08-30)
    and only the fingerprints are missing; it is wrong after any change to
    the documents since, which a full rebuild is for.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    if not Path(path).is_file():
        return 0
    today = {uid: doc_fingerprint(doc, model) for uid, _, doc in work_documents(conn)
             if doc.strip()}
    table = pq.read_table(path)
    uids = table.column("item_uid").to_pylist()
    old = (table.column("doc_hash").to_pylist() if "doc_hash" in table.column_names
           else [None] * len(uids))
    new = [h if h is not None else today.get(u) for u, h in zip(uids, old, strict=True)]
    adopted = sum(1 for a, b in zip(old, new, strict=True) if a is None and b is not None)
    out = pa.table({
        "item_uid": table.column("item_uid"),
        "domain": table.column("domain"),
        "vector": table.column("vector"),
        "doc_hash": pa.array(new, type=pa.string()),
    })
    partial = Path(path).with_suffix(Path(path).suffix + ".part")
    pq.write_table(out, partial, compression="zstd")
    partial.replace(path)
    return adopted
