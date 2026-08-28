"""Jikan v4 (unofficial MyAnimeList API) ingester for the ``anime`` domain.

Rate limits: the public Jikan instance allows roughly 3 requests/second and
60/minute. Both are enforced by :class:`~recsys.ingest.ratelimit.RateLimiter`
before every request; 429 and 5xx responses retry with jittered exponential
backoff, honouring ``Retry-After`` when present.

Every response is written to the on-disk cache keyed by URL, so a second
``bootstrap.py`` run completes without touching the network.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any, AsyncIterator

import httpx

from recsys.config import Settings
from recsys.ingest.base import Ingester
from recsys.ingest.cache import DiskCache
from recsys.ingest.ratelimit import RateLimiter, backoff_delay, parse_retry_after
from recsys.log import get_logger
from recsys.net import enable_os_truststore
from recsys.schemas import Creator, UnifiedItem, utcnow

log = get_logger(__name__)

SOURCE = "jikan"
DOMAIN = "anime"

# MAL synopses carry an attribution footer that is noise for text similarity.
_MAL_FOOTER = re.compile(r"\s*\[(?:Written by|Source:)[^\]]*\]\s*$", re.IGNORECASE)

# Jikan groups descriptive labels across four parallel arrays; they are all
# just tags as far as the unified schema is concerned.
_TAG_FIELDS = ("genres", "explicit_genres", "themes", "demographics")


# --------------------------------------------------------------------------
# mapping (pure -- unit-tested against a saved fixture, no network)
# --------------------------------------------------------------------------


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = _MAL_FOOTER.sub("", value).strip()
    return text or None


def _names(entries: Any, role: str) -> list[Creator]:
    out: list[Creator] = []
    if isinstance(entries, list):
        for e in entries:
            name = e.get("name") if isinstance(e, dict) else None
            if isinstance(name, str) and name.strip():
                out.append(Creator(name=name.strip(), role=role))
    return out


def _pick_image(raw: dict[str, Any]) -> str | None:
    images = raw.get("images") or {}
    for fmt in ("jpg", "webp"):
        block = images.get(fmt) or {}
        for key in ("large_image_url", "image_url", "small_image_url"):
            url = block.get(key)
            if isinstance(url, str) and url.strip():
                return url.strip()
    return None


def _pick_year(raw: dict[str, Any]) -> int | None:
    year = raw.get("year")
    if isinstance(year, int) and 1000 <= year <= 2100:
        return year
    prop = ((raw.get("aired") or {}).get("prop") or {}).get("from") or {}
    year = prop.get("year")
    if isinstance(year, int) and 1000 <= year <= 2100:
        return year
    return None


def _alt_titles(raw: dict[str, Any], canonical: str) -> list[str]:
    alts: list[str] = []
    for key in ("title_english", "title_japanese"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            alts.append(value.strip())
    for entry in raw.get("titles") or []:
        if isinstance(entry, dict):
            value = entry.get("title")
            if isinstance(value, str) and value.strip():
                alts.append(value.strip())
    for value in raw.get("title_synonyms") or []:
        if isinstance(value, str) and value.strip():
            alts.append(value.strip())
    return [t for t in alts if t != canonical]


def _external_ids(raw: dict[str, Any]) -> dict[str, str]:
    """MAL id plus whatever cross-references this endpoint happens to expose.

    Phase 1's cross-domain linker consumes this dict, so anything that could
    later join to another source is worth carrying now.
    """
    ids: dict[str, str] = {}
    mal_id = raw.get("mal_id")
    if mal_id is not None:
        ids["mal_id"] = str(mal_id)
    url = raw.get("url")
    if isinstance(url, str) and url:
        ids["mal_url"] = url
    youtube_id = (raw.get("trailer") or {}).get("youtube_id")
    if isinstance(youtube_id, str) and youtube_id:
        ids["youtube_id"] = youtube_id
    return ids


def map_jikan_anime(
    raw: dict[str, Any], *, fetched_at: datetime | None = None
) -> UnifiedItem | None:
    """Map one Jikan anime record to a UnifiedItem. None = unusable, skip it."""
    mal_id = raw.get("mal_id")
    if mal_id is None:
        return None

    # Checked for blankness rather than truthiness: a whitespace-only `title`
    # is truthy and would short-circuit an `or` chain, hiding a perfectly good
    # title_english behind it.
    canonical = None
    for candidate in (raw.get("title"), raw.get("title_english"), raw.get("title_japanese")):
        if isinstance(candidate, str) and candidate.strip():
            canonical = candidate
            break
    if canonical is None:
        for entry in raw.get("titles") or []:
            if isinstance(entry, dict) and entry.get("type") == "Default":
                canonical = entry.get("title")
                break
    if not isinstance(canonical, str) or not canonical.strip():
        return None
    canonical = canonical.strip()

    tags: list[str] = []
    for field in _TAG_FIELDS:
        tags.extend(c.name for c in _names(raw.get(field), "tag"))
    # `type` (TV / Movie / OVA) is a genuinely useful facet cross-domain.
    if isinstance(raw.get("type"), str) and raw["type"].strip():
        tags.append(raw["type"].strip())

    creators = _names(raw.get("studios"), "studio") + _names(raw.get("producers"), "producer")

    score = raw.get("score")
    # Jikan scores are already on MAL's 0-10 scale, which is the unified scale.
    mean_rating = float(score) if isinstance(score, (int, float)) else None
    if mean_rating is not None and not (0.0 <= mean_rating <= 10.0):
        mean_rating = None

    members = raw.get("members")

    return UnifiedItem(
        domain=DOMAIN,
        source=SOURCE,
        source_id=str(mal_id),
        title=canonical,
        alt_titles=_alt_titles(raw, canonical),
        year=_pick_year(raw),
        creators=creators,
        tags=tags,
        description=_clean_text(raw.get("synopsis")),
        external_ids=_external_ids(raw),
        # Raw MAL member count: a within-source size signal, not comparable to
        # another source's popularity number.
        popularity=float(members) if isinstance(members, (int, float)) else None,
        mean_rating=mean_rating,
        n_ratings=raw.get("scored_by") if isinstance(raw.get("scored_by"), int) else None,
        image_url=_pick_image(raw),
        fetched_at=fetched_at or utcnow(),
        raw_json=raw,
    )


# --------------------------------------------------------------------------
# ingester
# --------------------------------------------------------------------------


class JikanAnimeIngester(Ingester):
    name = "anime_jikan"
    domain = DOMAIN
    source = SOURCE

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.base_url = settings.jikan_base_url.rstrip("/")
        self.target_count = settings.jikan_target_count
        self.page_size = max(1, min(25, settings.jikan_page_size))  # Jikan caps at 25
        self.max_retries = settings.jikan_max_retries
        self.limiter = RateLimiter(settings.jikan_rate_per_sec, settings.jikan_rate_per_min)
        self.cache = DiskCache(settings.cache_dir, SOURCE, settings.http_cache_ttl_days)
        self._client: httpx.AsyncClient | None = None

    # ---- http -------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Must happen before the first TLS handshake.
            enable_os_truststore()
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                headers={"Accept": "application/json", "User-Agent": "recsys-loom/0.1 (local)"},
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_json(self, url: str) -> dict[str, Any]:
        cached = self.cache.get(url)
        if cached is not None:
            self.n_cached += 1
            return cached

        client = self._get_client()
        last_error: str = "unknown"

        for attempt in range(self.max_retries):
            await self.limiter.acquire()
            try:
                resp = await client.get(url)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                delay = backoff_delay(attempt)
                log.warning("%s -- retrying in %.1fs (%d/%d)",
                            last_error, delay, attempt + 1, self.max_retries)
                await asyncio.sleep(delay)
                continue

            if resp.status_code == 200:
                payload = resp.json()
                self.cache.set(url, payload)
                return payload

            if resp.status_code == 429 or resp.status_code >= 500:
                # 429 means the shared public quota is exhausted, not that our
                # own limiter is wrong -- back off and let it drain.
                delay = parse_retry_after(resp.headers.get("Retry-After"))
                if delay is None:
                    delay = backoff_delay(attempt, base=2.0 if resp.status_code == 429 else 1.0)
                last_error = f"HTTP {resp.status_code}"
                log.warning("%s from %s -- backing off %.1fs (%d/%d)",
                            last_error, url, delay, attempt + 1, self.max_retries)
                await asyncio.sleep(delay)
                continue

            # 400/404 and friends are permanent for this URL.
            raise RuntimeError(f"Jikan returned HTTP {resp.status_code} for {url}")

        raise RuntimeError(f"Jikan request failed after {self.max_retries} attempts "
                           f"({last_error}): {url}")

    # ---- Ingester contract ------------------------------------------------

    async def fetch(self) -> AsyncIterator[dict[str, Any]]:
        """Page through /top/anime until the target count is reached."""
        run_fetched_at = utcnow()
        self._fetched_at = run_fetched_at

        page = 1
        yielded = 0
        seen: set[Any] = set()

        while yielded < self.target_count:
            url = f"{self.base_url}/top/anime?page={page}&limit={self.page_size}"
            payload = await self._get_json(url)

            data = payload.get("data") or []
            if not data:
                log.info("jikan: page %d returned no data; stopping", page)
                break

            for raw in data:
                mal_id = raw.get("mal_id") if isinstance(raw, dict) else None
                # Upstream ranking can shift between page requests and surface
                # the same title twice.
                if mal_id in seen:
                    continue
                seen.add(mal_id)
                yield raw
                yielded += 1
                if yielded >= self.target_count:
                    break

            pagination = payload.get("pagination") or {}
            if not pagination.get("has_next_page"):
                log.info("jikan: reached last page (%d)", page)
                break
            page += 1

        log.info(
            "jikan: fetched %d records over %d page(s) (%d cache hits, %d misses)",
            yielded, page, self.cache.hits, self.cache.misses,
        )

    def map_item(self, raw: dict[str, Any]) -> UnifiedItem | None:
        return map_jikan_anime(raw, fetched_at=getattr(self, "_fetched_at", None))
