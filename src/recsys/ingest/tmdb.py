"""Shared TMDB machinery: one client, one mapper, the movie and tv domains.

TMDB serves films and television through parallel endpoints with the same shape,
so the client, the mapping and the two-phase fetch live here and the domain
modules stay thin -- the same split ``anilist.py`` uses. ``movie_tmdb.py`` and
``tv_tmdb.py`` differ in the endpoint path and in four field names that TMDB
spells differently for the two media.

**Two phases, because the list endpoints are not enough.** ``/discover`` returns
20 results a page and gives titles, an overview and numeric genre ids -- but no
keywords, no credits and no IMDb id. The per-item detail endpoint returns all of
them in a single request via ``append_to_response``, so this enumerates ids from
discover and then fetches each one. It costs a request per item and is worth it:
keywords are the whole reason to prefer TMDB over the ratings dumps.

``genres`` alone would be a thin vocabulary -- 19 of them for film, against
AniList's hundreds of ranked tags. ``keywords`` is TMDB's equivalent of those:
*Inception* comes back with ``dreams``, ``heist``, ``memory``, ``dream world``,
and *Breaking Bad* with ``drug dealer``, ``terminal illness``, ``dark comedy``.
Cross-domain matching works on attributes, and those are the attributes.

**No credential reaches disk.** A v4 read token is preferred and travels in an
``Authorization`` header, so it never appears in a URL at all; the v3 key is the
fallback and is a query parameter, which the cache would otherwise store in
plaintext inside every envelope, so it is stripped before the cache key is
built. Nothing under ``DATA_DIR`` and nothing in the logs contains either.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, AsyncIterator, ClassVar, Iterable

import httpx

from recsys.config import Settings
from recsys.ingest.base import Ingester
from recsys.ingest.cache import DiskCache
from recsys.ingest.ratelimit import RateLimiter, backoff_delay, parse_retry_after
from recsys.log import get_logger
from recsys.net import enable_os_truststore
from recsys.schemas import Creator, UnifiedItem, utcnow

log = get_logger(__name__)

SOURCE = "tmdb"

#: TMDB refuses to paginate past 500, and a page holds 20, so one `/discover`
#: sort can enumerate at most 10,000 items.
MAX_DISCOVER_PAGE = 500
PAGE_SIZE = 20

#: Cast credited beyond this are extras for our purposes, and a long tail of
#: bit-part actors would dilute the "same person" bridge.
MAX_CAST = 5


# --------------------------------------------------------------------------
# mapping (pure -- unit-tested against saved fixtures, no network)
# --------------------------------------------------------------------------


def _year(raw: dict[str, Any], date_field: str) -> int | None:
    text = raw.get(date_field)
    if isinstance(text, str) and len(text) >= 4 and text[:4].isdigit():
        year = int(text[:4])
        if 1000 <= year <= 2100:
            return year
    return None


def _names(entries: Any, role: str, limit: int | None = None) -> list[Creator]:
    out: list[Creator] = []
    for entry in entries if isinstance(entries, list) else []:
        name = entry.get("name") if isinstance(entry, dict) else None
        if isinstance(name, str) and name.strip():
            out.append(Creator(name=name.strip(), role=role))
        if limit is not None and len(out) >= limit:
            break
    return out


def _creators(raw: dict[str, Any]) -> list[Creator]:
    """Whoever authored it, then the billed cast.

    Films name a Director in the crew; television names ``created_by``. Both are
    the "same person" bridge, so principals come first and the cast follows in
    billing order.
    """
    credits = raw.get("credits") or {}
    crew = credits.get("crew") if isinstance(credits.get("crew"), list) else []

    out = _names(raw.get("created_by"), "creator")
    out += [
        Creator(name=c["name"].strip(), role="director")
        for c in crew
        if isinstance(c, dict)
        and c.get("job") == "Director"
        and isinstance(c.get("name"), str)
        and c["name"].strip()
    ]

    seen = {c.name for c in out}
    n_cast = 0
    for actor in _names(credits.get("cast"), "cast"):
        if actor.name in seen:
            continue
        seen.add(actor.name)
        out.append(actor)
        n_cast += 1
        if n_cast >= MAX_CAST:
            break
    return out


def _tags(raw: dict[str, Any], keyword_key: str) -> list[str]:
    """Genres plus keywords.

    Genres alone are 19 buckets and near-useless for matching; keywords are what
    make a film comparable to an anime on anything finer than "Action".
    """
    tags = [
        g["name"].strip()
        for g in (raw.get("genres") or [])
        if isinstance(g, dict) and isinstance(g.get("name"), str) and g["name"].strip()
    ]
    block = raw.get("keywords") or {}
    # TMDB spells the container differently for film and television.
    entries = block.get(keyword_key) if isinstance(block, dict) else None
    tags += [
        k["name"].strip()
        for k in (entries or [])
        if isinstance(k, dict) and isinstance(k.get("name"), str) and k["name"].strip()
    ]
    return tags


def _external_ids(raw: dict[str, Any], domain: str) -> dict[str, str]:
    """``imdb_id`` is the cross-reference worth carrying.

    It is what a future IMDb or MovieLens source would join on -- MovieLens
    ships ``links.csv`` mapping its own ids to both IMDb and TMDB.
    """
    ids: dict[str, str] = {"tmdb_id": str(raw["id"])}
    imdb = raw.get("imdb_id") or ((raw.get("external_ids") or {}).get("imdb_id"))
    if isinstance(imdb, str) and imdb.strip():
        ids["imdb_id"] = imdb.strip()
    path = "tv" if domain == "tv" else "movie"
    ids["tmdb_url"] = f"https://www.themoviedb.org/{path}/{raw['id']}"
    return ids


def map_tmdb(
    raw: dict[str, Any],
    domain: str,
    *,
    title_field: str,
    original_title_field: str,
    date_field: str,
    keyword_key: str,
    image_base: str,
    min_votes: int = 0,
    fetched_at: datetime | None = None,
) -> UnifiedItem | None:
    """Map one TMDB detail record to a UnifiedItem. None = unusable, skip it."""
    if not isinstance(raw, dict) or raw.get("id") is None:
        return None

    # Backstops for what `/discover` filters at the source, because a cached
    # payload from before a filter existed would otherwise slip through.
    if raw.get("adult"):
        return None
    # `video: true` marks a direct-to-video record rather than a release.
    if raw.get("video"):
        return None
    if isinstance(min_votes, int) and (raw.get("vote_count") or 0) < min_votes:
        return None

    canonical = None
    for candidate in (raw.get(title_field), raw.get(original_title_field)):
        if isinstance(candidate, str) and candidate.strip():
            canonical = candidate.strip()
            break
    if canonical is None:
        return None

    original = raw.get(original_title_field)
    alt_titles = (
        [original.strip()]
        if isinstance(original, str) and original.strip() and original.strip() != canonical
        else []
    )

    vote_average = raw.get("vote_average")
    # TMDB votes are already 0-10, which is the unified scale.
    mean_rating = (
        float(vote_average) if isinstance(vote_average, (int, float)) else None
    )
    if mean_rating is not None and not (0.0 <= mean_rating <= 10.0):
        mean_rating = None

    # `vote_count`, not TMDB's own `popularity`. That field is a volatile
    # trending score recomputed daily -- it measures attention this week, not
    # audience size, and would make the catalog's ordering shuffle on every
    # re-ingest. `popularity.py` calibrates whatever we store here anyway.
    votes = raw.get("vote_count")
    votes = int(votes) if isinstance(votes, (int, float)) and votes > 0 else None

    poster = raw.get("poster_path")
    overview = raw.get("overview")

    return UnifiedItem(
        domain=domain,
        source=SOURCE,
        source_id=str(raw["id"]),
        title=canonical,
        alt_titles=alt_titles,
        year=_year(raw, date_field),
        creators=_creators(raw),
        tags=_tags(raw, keyword_key),
        description=overview.strip() if isinstance(overview, str) and overview.strip() else None,
        external_ids=_external_ids(raw, domain),
        popularity=float(votes) if votes else None,
        mean_rating=mean_rating,
        n_ratings=votes,
        image_url=f"{image_base}{poster}" if isinstance(poster, str) and poster else None,
        fetched_at=fetched_at or utcnow(),
        raw_json=raw,
    )


# --------------------------------------------------------------------------
# ingester base
# --------------------------------------------------------------------------


class TmdbIngester(Ingester):
    """Enumerate ids from /discover, then fetch each item's detail."""

    source = SOURCE

    #: "movie" or "tv" -- TMDB's path segment for this medium.
    media_path: ClassVar[str]
    #: Field names TMDB spells differently for film and television.
    title_field: ClassVar[str]
    original_title_field: ClassVar[str]
    date_field: ClassVar[str]
    keyword_key: ClassVar[str]
    #: Extra blocks to fold into the detail request.
    append: ClassVar[str] = "credits,keywords"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.base_url = settings.tmdb_base_url.rstrip("/")
        self.api_key = settings.tmdb_api_key
        self.read_token = settings.tmdb_read_token
        self.image_base = settings.tmdb_image_base.rstrip("/") + "/"
        self.target_count = settings.tmdb_target_count
        self.min_votes = settings.tmdb_min_votes
        self.max_retries = settings.tmdb_max_retries
        self.limiter = RateLimiter(settings.tmdb_rate_per_sec, settings.tmdb_rate_per_min)
        self.cache = DiskCache(settings.cache_dir, SOURCE, settings.http_cache_ttl_days)
        self._client: httpx.AsyncClient | None = None

    # ---- http -------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            enable_os_truststore()  # must precede the first TLS handshake
            headers = {
                "Accept": "application/json",
                "User-Agent": "recsys-loom/0.1 (local)",
            }
            # Preferred: the secret lives in a header and never touches a URL,
            # a log line or a cache envelope.
            if self.read_token:
                headers["Authorization"] = f"Bearer {self.read_token}"
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                headers=headers,
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _cache_key(self, path: str, params: dict[str, Any]) -> str:
        """Identify a request without naming the secret that authorises it.

        DiskCache stores the string it is keyed on inside every envelope, so an
        api_key left in here would be written to disk in plaintext for every
        cached response.
        """
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "api_key")
        return f"{self.base_url}{path}?{query}"

    async def _get_json(self, path: str, **params: Any) -> dict[str, Any]:
        key = self._cache_key(path, params)
        cached = self.cache.get(key)
        if cached is not None:
            self.n_cached += 1
            return cached

        client = self._get_client()
        # Only fall back to the query-string key when there is no bearer token.
        request_params = dict(params)
        if not self.read_token:
            request_params["api_key"] = self.api_key
        last_error = "unknown"

        for attempt in range(self.max_retries):
            await self.limiter.acquire()
            try:
                resp = await client.get(f"{self.base_url}{path}", params=request_params)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                delay = backoff_delay(attempt)
                log.warning("%s -- retrying in %.1fs (%d/%d)",
                            last_error, delay, attempt + 1, self.max_retries)
                await asyncio.sleep(delay)
                continue

            if resp.status_code == 200:
                payload = resp.json()
                self.cache.set(key, payload)
                return payload

            if resp.status_code == 429 or resp.status_code >= 500:
                delay = parse_retry_after(resp.headers.get("Retry-After"))
                if delay is None:
                    delay = backoff_delay(attempt, base=2.0 if resp.status_code == 429 else 1.0)
                last_error = f"HTTP {resp.status_code}"
                log.warning("%s from TMDB -- backing off %.1fs (%d/%d)",
                            last_error, delay, attempt + 1, self.max_retries)
                await asyncio.sleep(delay)
                continue

            if resp.status_code == 401:
                # Never echo the key, not even truncated.
                raise RuntimeError(
                    "TMDB rejected the credential (HTTP 401). Check "
                    "TMDB_READ_TOKEN / TMDB_API_KEY in .env"
                )
            if resp.status_code == 404:
                return {}  # an id /discover listed but detail does not serve

            # The path is a class constant, so a 4xx here is a bug, not bad input.
            raise RuntimeError(f"TMDB returned HTTP {resp.status_code} for {path}")

        raise RuntimeError(f"TMDB request failed after {self.max_retries} attempts "
                           f"({last_error}): {path}")

    # ---- fetch ------------------------------------------------------------

    async def _discover_ids(self) -> list[int]:
        """Enumerate ids, most popular first, up to the target count.

        TMDB caps pagination at 500 pages of 20, so one sort reaches 10,000
        items. Going deeper means partitioning by year or genre, which is a
        problem for the day the catalog needs to be bigger than that.
        """
        wanted = self.target_count or MAX_DISCOVER_PAGE * PAGE_SIZE
        pages = min(MAX_DISCOVER_PAGE, (wanted + PAGE_SIZE - 1) // PAGE_SIZE)
        ids: list[int] = []
        seen: set[int] = set()

        for page in range(1, pages + 1):
            payload = await self._get_json(
                f"/discover/{self.media_path}",
                page=page,
                sort_by="popularity.desc",
                include_adult="false",
                # Filtered at the source: every row rejected here is a detail
                # request never made. A title with a handful of votes has no
                # audience signal and a score built on nothing -- measured, the
                # median TMDB show had 28 votes and the bottom tenth had 2.
                **{"vote_count.gte": self.min_votes},
            )
            results = payload.get("results") or []
            if not results:
                log.info("%s: /discover ran out at page %d", self.name, page)
                break
            for entry in results:
                tmdb_id = entry.get("id") if isinstance(entry, dict) else None
                # The ranking shifts between requests, so the same title can
                # appear on two pages.
                if isinstance(tmdb_id, int) and tmdb_id not in seen:
                    seen.add(tmdb_id)
                    ids.append(tmdb_id)
            if len(ids) >= wanted:
                break
            if page % 50 == 0:
                log.info("%s: discovered %d ids over %d pages", self.name, len(ids), page)

        return ids[:wanted]

    async def fetch(self) -> AsyncIterator[dict[str, Any]]:
        if not (self.read_token or self.api_key):
            raise RuntimeError(
                "No TMDB credential. Set TMDB_READ_TOKEN (preferred) or "
                "TMDB_API_KEY in .env; both are free at "
                "https://www.themoviedb.org/settings/api"
            )

        self._fetched_at = utcnow()
        ids = await self._discover_ids()
        log.info("%s: %d ids to fetch in detail", self.name, len(ids))

        for n, tmdb_id in enumerate(ids, start=1):
            detail = await self._get_json(
                f"/{self.media_path}/{tmdb_id}", append_to_response=self.append
            )
            if detail:
                yield detail
            if n % 500 == 0:
                log.info("%s: %d/%d detail records (%d cache hits)",
                         self.name, n, len(ids), self.cache.hits)

        log.info("%s: fetched %d of %d ids", self.name, len(ids), len(ids))

    def map_item(self, raw: dict[str, Any]) -> UnifiedItem | None:
        return map_tmdb(
            raw,
            self.domain,
            title_field=self.title_field,
            original_title_field=self.original_title_field,
            date_field=self.date_field,
            keyword_key=self.keyword_key,
            image_base=self.image_base,
            min_votes=self.min_votes,
            fetched_at=getattr(self, "_fetched_at", None),
        )


def iter_creator_names(items: Iterable[UnifiedItem]) -> set[str]:
    """Every credited name in a batch. Small helper for tests and diagnostics."""
    return {c.name for item in items for c in item.creators}
