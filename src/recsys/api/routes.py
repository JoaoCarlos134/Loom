"""HTTP API. Thin: routes validate input, call db helpers, return models."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from recsys import __version__
from recsys.config import Settings, get_settings
from recsys.db import (
    count_items,
    domain_stats,
    get_item,
    last_ingest_at,
    query_items,
)
from recsys.relations import franchise_representatives
from recsys.schemas import (
    DOMAINS,
    DomainStats,
    Health,
    ItemsPage,
    SimilarItem,
    SimilarPage,
    Stats,
    UnifiedItem,
)

router = APIRouter(prefix="/api", tags=["catalog"])


@lru_cache(maxsize=1)
def _similarity_index(path: Path):
    """The embedding matrix, loaded once per process and held.

    129 MB resident and 0.18 s to read, against 5 ms for a query -- so it is
    loaded lazily on the first request that needs it rather than at startup,
    which keeps a catalog-only session from paying for a model it never asks
    about. Cached on the path, so a test pointing at another DATA_DIR gets its
    own. ``None`` when the matrix has not been built yet.
    """
    if not Path(path).is_file():
        return None
    from recsys.models.similarity import SimilarityIndex

    return SimilarityIndex.load(Path(path))


@lru_cache(maxsize=1)
def _collaborative_index(path: Path):
    """The item-item neighbour table, loaded once per process. ``None`` if unbuilt."""
    if not Path(path).is_file():
        return None
    from recsys.models.collaborative import CollaborativeIndex

    return CollaborativeIndex.load(Path(path))


def db_conn() -> Iterator[sqlite3.Connection]:
    """One short-lived connection per request.

    SQLite connections are not safe to share across FastAPI's threadpool
    workers, and opening one costs microseconds against a local file.
    """
    from recsys.db import session

    with session() as conn:
        yield conn


Conn = Annotated[sqlite3.Connection, Depends(db_conn)]
Cfg = Annotated[Settings, Depends(get_settings)]


@router.get("/health", response_model=Health)
def health(conn: Conn) -> Health:
    """Liveness probe. app.py polls this before opening the desktop window."""
    try:
        n = count_items(conn)
        return Health(status="ok", version=__version__, db_ready=True, n_items=n)
    except sqlite3.Error:
        return Health(status="degraded", version=__version__, db_ready=False, n_items=0)


@router.get("/items", response_model=ItemsPage)
def list_items(
    conn: Conn,
    domain: str | None = Query(None, description="Filter to one domain"),
    q: str | None = Query(None, description="Full-text search over title + description"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ItemsPage:
    if domain is not None:
        domain = domain.strip().lower()
        if domain in ("", "all"):
            domain = None
        elif domain not in DOMAINS:
            raise HTTPException(422, f"unknown domain {domain!r}; expected one of {list(DOMAINS)}")

    q = q.strip() if q else None
    total, items = query_items(conn, domain=domain, q=q, limit=limit, offset=offset)
    return ItemsPage(total=total, limit=limit, offset=offset, items=items)


@router.get("/items/{item_uid:path}", response_model=UnifiedItem)
def read_item(item_uid: str, conn: Conn) -> UnifiedItem:
    # :path because item_uid is "domain:source:id" and some source ids contain
    # characters a plain path segment would mangle.
    item = get_item(conn, item_uid)
    if item is None:
        raise HTTPException(404, f"no item with uid {item_uid!r}")
    return item


@router.get("/similar/{item_uid:path}", response_model=SimilarPage)
def similar(
    item_uid: str,
    conn: Conn,
    cfg: Cfg,
    domain: str | None = Query(None, description="Restrict neighbours to one domain"),
    k: int = Query(10, ge=1, le=50),
    same_franchise: bool = Query(
        False, description="Include a work's own sequels, prequels and side stories"
    ),
    method: str = Query(
        "content", description="'content' (text embeddings) or 'cf' (who liked what)"
    ),
) -> SimilarPage:
    """The k works most like this one, optionally in another domain.

    Asking for the films nearest an anime is what makes Loom cross-domain
    rather than a related-items widget, and it is the only route that reads a
    model rather than the catalog.

    Two models answer it and they fail differently. ``content`` compares what
    works are *about* and will happily return something nobody enjoyed;
    ``cf`` compares who rated what and has never heard of a work nobody rated,
    nor of any domain but anime, because that is all the ratings corpus covers.
    """
    if method not in ("content", "cf"):
        raise HTTPException(422, f"unknown method {method!r}; expected 'content' or 'cf'")
    if domain is not None:
        domain = domain.strip().lower()
        if domain in ("", "all"):
            domain = None
        elif domain not in DOMAINS:
            raise HTTPException(422, f"unknown domain {domain!r}; expected one of {list(DOMAINS)}")

    if get_item(conn, item_uid) is None:
        raise HTTPException(404, f"no item with uid {item_uid!r}")

    # Over-fetch so franchise suppression cannot return fewer than k when there
    # are k genuine neighbours behind the sequels.
    suppress: set[str] = set()
    if not same_franchise:
        suppress = franchise_representatives(conn, item_uid)
    want = k + len(suppress)

    if method == "cf":
        cf = _collaborative_index(cfg.collaborative_path)
        if cf is None:
            raise HTTPException(
                503,
                f"no collaborative model at {cfg.collaborative_path}; "
                "run scripts/cf.py to build it",
            )
        # The index translates uid spaces itself; an empty list means this work
        # has no ratings, which is an answer rather than an error.
        found = cf.similar_to(conn, item_uid, k=want)
        if domain is not None:
            found = [n for n in found if n.item_uid.startswith(f"{domain}:")]
    else:
        index = _similarity_index(cfg.embeddings_path)
        if index is None:
            raise HTTPException(
                503,
                f"no embedding matrix at {cfg.embeddings_path}; "
                "run scripts/embed.py to build it",
            )
        if not index.has(item_uid):
            # In the catalog but not the matrix: the matrix is a snapshot and is
            # rebuilt separately, so this is staleness, not a missing item.
            raise HTTPException(
                409,
                f"{item_uid!r} is not in the embedding matrix; "
                "rebuild it with scripts/embed.py",
            )
        found = index.similar_to(item_uid, k=want, domain=domain)

    kept: list[Any] = []
    n_suppressed = 0
    for neighbour in found:
        if neighbour.item_uid in suppress:
            n_suppressed += 1
            continue
        if len(kept) == k:
            break
        kept.append(neighbour)

    items = {i.item_uid: i for i in (get_item(conn, n.item_uid) for n in kept) if i is not None}
    return SimilarPage(
        item_uid=item_uid,
        domain=domain,
        method=method,
        n_suppressed=n_suppressed,
        neighbours=[
            SimilarItem(score=n.score, item=items[n.item_uid])
            for n in kept
            if n.item_uid in items
        ],
    )


@router.get("/stats", response_model=Stats)
def stats(conn: Conn, cfg: Cfg) -> Stats:
    last = last_ingest_at(conn)
    return Stats(
        total_items=count_items(conn),
        by_domain=[DomainStats.model_validate(d) for d in domain_stats(conn)],
        last_ingest_at=datetime.fromisoformat(last) if last else None,
        db_path=str(cfg.db_path),
        region=cfg.region,
    )
