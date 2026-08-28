"""HTTP API. Thin: routes validate input, call db helpers, return models."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Iterator

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
from recsys.schemas import DOMAINS, Health, ItemsPage, Stats, UnifiedItem

router = APIRouter(prefix="/api", tags=["catalog"])


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


@router.get("/stats", response_model=Stats)
def stats(conn: Conn, cfg: Cfg) -> Stats:
    return Stats(
        total_items=count_items(conn),
        by_domain=domain_stats(conn),
        last_ingest_at=last_ingest_at(conn),
        db_path=str(cfg.db_path),
        region=cfg.region,
    )
