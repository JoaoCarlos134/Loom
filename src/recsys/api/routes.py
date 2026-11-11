"""HTTP API. Thin: routes validate input, call db helpers, return models."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from recsys import __version__
from recsys.config import Settings, get_settings
from recsys.db import (
    ITEM_SORTS,
    LIBRARY_SORTS,
    count_items,
    delete_rating,
    domain_stats,
    get_item,
    get_rating,
    last_ingest_at,
    query_items,
    query_ratings,
    rated_uids,
    set_rating,
    top_tags,
)
from recsys.franchise import Franchise, franchise_of
from recsys.models.profile import build_profile
from recsys.schemas import (
    DOMAINS,
    DomainStats,
    Health,
    ItemsPage,
    LibraryPage,
    RatedItem,
    RatingIn,
    RecommendPage,
    SimilarItem,
    SimilarPage,
    Stats,
    TagCount,
    UnifiedItem,
)

router = APIRouter(prefix="/api", tags=["catalog"])

#: Tags one listing may combine. Far past what a reader narrows by.
MAX_TAGS = 20


def _similarity_index(path: Path):
    """The embedding matrix, loaded once per process and held.

    129 MB resident and 0.18 s to read, against 5 ms for a query -- so it is
    loaded lazily on the first request that needs it rather than at startup,
    which keeps a catalog-only session from paying for a model it never asks
    about. Cached on the path, so a test pointing at another DATA_DIR gets its
    own. ``None`` when the matrix has not been built yet -- and that answer is
    not cached, so running embed.py while the app is up needs no restart.
    """
    if not Path(path).is_file():
        return None
    return _load_similarity_index(path)


@lru_cache(maxsize=1)
def _load_similarity_index(path: Path):
    from recsys.models.similarity import SimilarityIndex

    return SimilarityIndex.load(Path(path))


def _collaborative_index(path: Path):
    """The item-item neighbour table, loaded once per process. ``None`` if unbuilt.

    Same rule as ``_similarity_index``: a missing file is never cached.
    """
    if not Path(path).is_file():
        return None
    return _load_collaborative_index(path)


@lru_cache(maxsize=1)
def _load_collaborative_index(path: Path):
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


def _validated_domains(domain: Sequence[str]) -> list[str]:
    """Normalise and check a repeatable ``domain`` parameter. Empty means all.

    Repeatable so a reader can ask for anime and films and nothing else. An
    empty value or ``all`` anywhere in the list is no restriction, which is
    what a client that always sends the parameter needs.
    """
    wanted: dict[str, None] = {}
    for value in domain:
        value = value.strip().lower()
        if value in ("", "all"):
            return []
        if value not in DOMAINS:
            raise HTTPException(
                422, f"unknown domain {value!r}; expected one of {list(DOMAINS)}"
            )
        wanted.setdefault(value, None)
    # Every domain named is the same question as none named.
    return [] if len(wanted) == len(DOMAINS) else list(wanted)


def _only(domains: list[str]) -> str | None:
    """The one domain a response names, when the answer was restricted to one."""
    return domains[0] if len(domains) == 1 else None


@router.get("/health", response_model=Health)
def health(conn: Conn) -> Health:
    """Liveness probe. app.py polls this before opening the desktop window."""
    try:
        n = count_items(conn)
        return Health(status="ok", version=__version__, db_ready=True, n_items=n)
    except sqlite3.Error:
        return Health(status="degraded", version=__version__, db_ready=False, n_items=0)


def _validated_sort(sort: str | None, allowed: Sequence[str]) -> str | None:
    """Normalise and check a ``sort`` query parameter. ``None`` means the default."""
    if sort is None or not sort.strip():
        return None
    sort = sort.strip().lower()
    if sort not in allowed:
        raise HTTPException(422, f"unknown sort {sort!r}; expected one of {list(allowed)}")
    return sort


def _fresh(
    conn: sqlite3.Connection,
    fetch: Callable[[int], list[Any]],
    k: int,
    franchise: Franchise | None,
) -> tuple[list[tuple[Any, UnifiedItem]], int]:
    """The first ``k`` neighbours that are not more of the same, and how many were.

    ``fetch(n)`` returns the ``n`` nearest, ranked. A franchise cannot be taken
    out before ranking the way a rated uid can: half of it is recognised by
    title, which is only known once a candidate is in hand. So this over-fetches
    and widens until the page is full or the model has nothing further --
    rating one long-running series must not come back as a page of six.

    The count is of what was actually skipped on the way to ``k``, which is the
    number a reader can use: how much of this page would have been repeats.
    """
    items: dict[str, UnifiedItem | None] = {}
    n = k if franchise is None else 2 * k + 20
    while True:
        found = fetch(n)
        kept: list[tuple[Any, UnifiedItem]] = []
        n_suppressed = 0
        for neighbour in found:
            uid = neighbour.item_uid
            if uid not in items:
                items[uid] = get_item(conn, uid)
            item = items[uid]
            if item is None:
                continue  # in the model, gone from the catalog: a stale artifact
            if franchise is not None and franchise.covers(item):
                n_suppressed += 1
                continue
            kept.append((neighbour, item))
            if len(kept) == k:
                break
        if len(kept) == k or len(found) < n:
            return kept, n_suppressed
        n *= 2


@router.get("/items", response_model=ItemsPage)
def list_items(
    conn: Conn,
    domain: list[str] = Query(default_factory=list, description="Only these domains"),
    q: str | None = Query(None, description="Full-text search over title + description"),
    tag: list[str] = Query(default_factory=list, description="Carry every one of these tags"),
    year_min: int | None = Query(None, ge=1000, le=2100),
    year_max: int | None = Query(None, ge=1000, le=2100),
    min_rating: float | None = Query(None, ge=0.0, le=10.0),
    sort: str | None = Query(
        None, description=f"One of {list(ITEM_SORTS)}; default is relevance for a "
                          "search and popularity otherwise"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ItemsPage:
    domains = _validated_domains(domain)
    q = q.strip() if q else None
    # Tags are stored lowercased by the schema, so a filter is folded the same
    # way rather than silently matching nothing for "Sci-Fi".
    tags = [t.strip().lower() for t in tag if t.strip()]
    if len(tags) > MAX_TAGS:
        # Each tag is a subquery, and SQLite refuses an expression tree past a
        # depth it is not this route's business to discover with a 500.
        raise HTTPException(422, f"at most {MAX_TAGS} tags can be combined")
    total, items = query_items(
        conn, domain=domains, q=q, tags=tags, year_min=year_min, year_max=year_max,
        min_rating=min_rating, sort=_validated_sort(sort, ITEM_SORTS),
        limit=limit, offset=offset,
    )
    return ItemsPage(total=total, limit=limit, offset=offset, items=items)


@router.get("/tags", response_model=list[TagCount])
def list_tags(
    conn: Conn,
    domain: list[str] = Query(default_factory=list, description="Count within these"),
    q: str | None = Query(None, description="Only tags containing this text"),
    limit: int = Query(30, ge=1, le=200),
) -> list[TagCount]:
    """The most-carried tags, for a picker. Empty until the read view is built."""
    return [
        TagCount(tag=tag, n=n)
        for tag, n in top_tags(conn, domain=_validated_domains(domain), q=q, limit=limit)
    ]


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
    domain: list[str] = Query(default_factory=list, description="Only these domains"),
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
    nor of any domain its ratings corpus does not cover (anime and books).
    """
    if method not in ("content", "cf"):
        raise HTTPException(422, f"unknown method {method!r}; expected 'content' or 'cf'")
    domains = _validated_domains(domain)

    if get_item(conn, item_uid) is None:
        raise HTTPException(404, f"no item with uid {item_uid!r}")

    franchise = None if same_franchise else franchise_of(conn, [item_uid])
    fetch: Callable[[int], list[Any]]

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
        def fetch(n: int) -> list[Any]:
            found: list[Any] = list(cf.similar_to(conn, item_uid, k=n))
            if domains:
                found = [x for x in found if x.item_uid.split(":", 1)[0] in domains]
            return found
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

        def fetch(n: int) -> list[Any]:
            return list(index.similar_to(item_uid, k=n, domain=domains))

    kept, n_suppressed = _fresh(conn, fetch, k, franchise)
    return SimilarPage(
        item_uid=item_uid,
        domain=_only(domains),
        method=method,
        n_suppressed=n_suppressed,
        neighbours=[SimilarItem(score=n.score, item=item) for n, item in kept],
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


# --------------------------------------------------------------------------
# your library
#
# The only routes that write. Everything above reports what sources said;
# these record what *you* say, which is the one thing no ingester can supply
# and the signal every model above is waiting for.
# --------------------------------------------------------------------------


def _rated_item(conn: sqlite3.Connection, item_uid: str) -> RatedItem | None:
    """A work with your score on it, or ``None`` if you have not rated it."""
    scored = get_rating(conn, item_uid)
    if scored is None:
        return None
    rating, rated_at = scored
    item = get_item(conn, item_uid)
    if item is None:  # pragma: no cover - callers check the item exists first
        return None
    return RatedItem(
        rating=rating,
        rated_at=datetime.fromisoformat(rated_at) if rated_at else None,
        item=item,
    )


@router.put("/ratings/{item_uid:path}", response_model=RatedItem)
def rate_item(item_uid: str, body: RatingIn, conn: Conn) -> RatedItem:
    """Rate a work 1-10. Idempotent: rating it again is a correction, not a second opinion.

    PUT rather than POST because a rating is addressed by the thing it is about
    and there can only be one of yours -- ``upsert_ratings`` was already an
    upsert, and the route should not pretend otherwise.
    """
    if get_item(conn, item_uid) is None:
        raise HTTPException(404, f"no item with uid {item_uid!r}")

    set_rating(conn, item_uid, body.rating)
    rated = _rated_item(conn, item_uid)
    assert rated is not None  # just written
    return rated


@router.get("/ratings/{item_uid:path}", response_model=RatedItem)
def read_rating(item_uid: str, conn: Conn) -> RatedItem:
    """Your score for one work, so opening it shows what you already said."""
    rated = _rated_item(conn, item_uid)
    if rated is None:
        raise HTTPException(404, f"{item_uid!r} is not rated")
    return rated


@router.delete("/ratings/{item_uid:path}", status_code=204)
def unrate_item(item_uid: str, conn: Conn) -> Response:
    """Forget a rating. 404 if there was nothing to forget, so a UI can tell."""
    if not delete_rating(conn, item_uid):
        raise HTTPException(404, f"{item_uid!r} is not rated")
    return Response(status_code=204)


@router.get("/library", response_model=LibraryPage)
def library(
    conn: Conn,
    domain: list[str] = Query(default_factory=list, description="Only these domains"),
    q: str | None = Query(None, description="Search within what you have rated"),
    sort: str | None = Query(None, description=f"One of {list(LIBRARY_SORTS)}"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> LibraryPage:
    """What you have rated, newest first unless ``sort`` says otherwise."""
    total, rows = query_ratings(
        conn, domain=_validated_domains(domain), q=q.strip() if q else None,
        sort=_validated_sort(sort, tuple(LIBRARY_SORTS)) or "recent",
        limit=limit, offset=offset,
    )
    return LibraryPage(
        total=total,
        limit=limit,
        offset=offset,
        items=[
            RatedItem(
                rating=rating,
                rated_at=datetime.fromisoformat(rated_at) if rated_at else None,
                item=item,
            )
            for rating, rated_at, item in rows
        ],
    )


@router.get("/recommend", response_model=RecommendPage)
def recommend(
    conn: Conn,
    cfg: Cfg,
    domain: list[str] = Query(default_factory=list, description="Only these domains"),
    k: int = Query(20, ge=1, le=200),
    same_franchise: bool = Query(
        False, description="Include sequels and side stories of things you rated"
    ),
) -> RecommendPage:
    """Works nearest *your taste*, optionally in a domain you have never rated in.

    This is ``/api/similar`` with the query replaced by a profile: instead of
    "what is like this one thing", "what is like the things you liked". Asking
    for ``domain=book`` having rated only anime is the cross-domain bridge
    doing the job it was built for, and it needs no ratings in the target
    domain at all -- which is exactly what collaborative filtering cannot do,
    since no user in the ratings corpus has rated across domains.
    """
    domains = _validated_domains(domain)
    index = _similarity_index(cfg.embeddings_path)
    if index is None:
        raise HTTPException(
            503,
            f"no embedding matrix at {cfg.embeddings_path}; run scripts/embed.py to build it",
        )

    ratings = rated_uids(conn)
    profile = build_profile(index, ratings)
    if profile is None:
        raise HTTPException(
            409,
            "nothing to recommend from: rate something first, or rebuild the "
            "embedding matrix so the things you rated are in it",
        )

    # Never recommend what you have already rated, nor -- as with /api/similar
    # -- more of the same franchise, which is the nearest neighbour of anything
    # you liked and the least useful answer to "what next".
    rated = set(ratings)
    franchise = None if same_franchise else franchise_of(conn, ratings)

    def fetch(n: int) -> list[Any]:
        return list(
            index.similar_to_vector(profile.vector, k=n, domain=domains, exclude=rated)
        )

    kept, n_suppressed = _fresh(conn, fetch, k, franchise)
    return RecommendPage(
        domain=_only(domains),
        n_rated=profile.n_rated,
        n_used=profile.n_used,
        n_suppressed=n_suppressed,
        neighbours=[SimilarItem(score=n.score, item=item) for n, item in kept],
    )
