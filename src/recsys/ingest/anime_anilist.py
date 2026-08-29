"""AniList GraphQL ingester for the ``anime`` domain -- the source that has text.

Added for one reason the Kaggle dump cannot satisfy: **synopses**. Every anime
table in that archive ships the same 31 columns and none of them is a plot
summary, so ``description`` on the CSV half of the catalog is ``background``
(production trivia) at 7.3% coverage. Content-based similarity across domains
needs real prose, and this is where it comes from.

Two things arrive with it that matter as much:

* **A far richer tag vocabulary.** MAL exposes ~43 genres; AniList adds ranked
  descriptive tags -- *Steins;Gate* comes back with ``Time Manipulation``,
  ``Time Loop``, ``Alternate Universe``, ``Memory Manipulation``. Cross-domain
  matching works on attributes, and these are the attributes.
* **Adaptation edges.** ``relations`` carries ``SOURCE`` / ``ADAPTATION`` links
  to the manga or light novel a series came from. Nothing consumes them yet, so
  they are simply kept in ``raw_json`` -- which is exactly what ``raw_json`` is
  for, and means mining them later costs no further requests.

**This is a third source, not a rewrite of the other two.** Rows land under
``anime:anilist:{id}`` beside ``anime:mal:{id}``, because ``item_uid`` is derived
and no source may write into another source's row. Every record carries
``external_ids.mal_id`` from AniList's own ``idMal`` field, which is what
``recsys.link`` joins on to collapse the duplicates out of listings. The join is
an exact integer match, not fuzzy title matching.

Which ids get fetched is driven by the catalog: the MAL ids already present are
read once, most-popular first, and requested 50 at a time. So this ingester
expects ``anime_mal_csv`` to have run before it, and enriches precisely the
catalog that exists rather than importing a second one.

Rate limit: the public endpoint advertised ``x-ratelimit-limit: 30`` per minute
when this was written, well below Jikan's. ~290 requests covers 14.5k anime, so
a full run is roughly ten minutes and every response is cached on disk.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime
from typing import Any, AsyncIterator, Iterator

import httpx

from recsys.config import Settings
from recsys.ingest.base import Ingester
from recsys.ingest.cache import DiskCache
from recsys.ingest.ratelimit import RateLimiter, backoff_delay, parse_retry_after
from recsys.log import get_logger
from recsys.net import enable_os_truststore
from recsys.schemas import Creator, UnifiedItem, utcnow

log = get_logger(__name__)

SOURCE = "anilist"
DOMAIN = "anime"

#: AniList caps a page at 50 records, and `idMal_in` accepts a list, so one
#: request covers 50 anime.
MAX_PER_PAGE = 50

QUERY = """
query ($ids: [Int], $perPage: Int) {
  Page(page: 1, perPage: $perPage) {
    media(idMal_in: $ids, type: ANIME) {
      id
      idMal
      siteUrl
      format
      seasonYear
      popularity
      averageScore
      startDate { year }
      title { romaji english native }
      synonyms
      description(asHtml: false)
      genres
      tags { name rank isMediaSpoiler }
      studios { edges { isMain node { name } } }
      coverImage { large }
      stats { scoreDistribution { score amount } }
      relations {
        edges { relationType node { id idMal type format title { romaji } } }
      }
    }
  }
}
"""

# Descriptions arrive with light HTML even at asHtml:false, and often end with a
# sourcing note that is noise for text similarity.
_TAG_RE = re.compile(r"<[^>]+>")
_SOURCE_NOTE = re.compile(r"\s*\((?:Source|Note)\s*:[^)]*\)\s*$", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t]*\n[ \t]*")


# --------------------------------------------------------------------------
# mapping (pure -- unit-tested against a saved fixture, no network)
# --------------------------------------------------------------------------


def clean_description(value: Any) -> str | None:
    """Strip AniList's markup down to plain text."""
    if not isinstance(value, str):
        return None
    text = _TAG_RE.sub(" ", value)
    text = html.unescape(text)
    text = _WS_RE.sub("\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    text = _SOURCE_NOTE.sub("", text).strip()
    return text or None


def _titles(raw: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Pick a canonical title and collect the rest.

    Checked for blankness rather than truthiness, matching the Jikan mapper: a
    whitespace-only romaji title is truthy and would hide a good english one.
    """
    block = raw.get("title") or {}
    ordered = [block.get("romaji"), block.get("english"), block.get("native")]
    canonical = None
    for candidate in ordered:
        if isinstance(candidate, str) and candidate.strip():
            canonical = candidate.strip()
            break
    if canonical is None:
        return None, []

    alts = [
        t.strip()
        for t in ordered + list(raw.get("synonyms") or [])
        if isinstance(t, str) and t.strip() and t.strip() != canonical
    ]
    return canonical, alts


def _tags(raw: dict[str, Any], min_rank: int) -> list[str]:
    """Genres plus descriptive tags, minus spoilers and minus noise.

    Spoiler tags are excluded outright -- they describe the ending, and this
    text reaches a UI. Low-ranked tags are dropped because AniList's long tail
    is community-suggested and weakly agreed; the full list survives in
    ``raw_json`` if a later phase wants it.
    """
    out = [g for g in (raw.get("genres") or []) if isinstance(g, str) and g.strip()]
    for tag in raw.get("tags") or []:
        if not isinstance(tag, dict) or tag.get("isMediaSpoiler"):
            continue
        name = tag.get("name")
        rank = tag.get("rank")
        if isinstance(name, str) and name.strip() and (rank is None or rank >= min_rank):
            out.append(name.strip())
    # Matches the other anime mappers: the format is a useful cross-domain facet.
    if isinstance(raw.get("format"), str) and raw["format"].strip():
        out.append(raw["format"].strip())
    return out


def _creators(raw: dict[str, Any]) -> list[Creator]:
    out: list[Creator] = []
    for edge in ((raw.get("studios") or {}).get("edges") or []):
        if not isinstance(edge, dict):
            continue
        name = ((edge.get("node") or {}).get("name") or "").strip()
        if name:
            out.append(Creator(name=name, role="studio" if edge.get("isMain") else "producer"))
    return out


def _n_ratings(raw: dict[str, Any]) -> int | None:
    """How many people actually scored it, summed from the score histogram.

    AniList's ``popularity`` counts users who put it on a list, which is a
    different and larger number -- that one is the popularity signal.
    """
    dist = ((raw.get("stats") or {}).get("scoreDistribution")) or []
    total = sum(d.get("amount") or 0 for d in dist if isinstance(d, dict))
    return total or None


def _year(raw: dict[str, Any]) -> int | None:
    for candidate in (raw.get("seasonYear"), (raw.get("startDate") or {}).get("year")):
        if isinstance(candidate, int) and 1000 <= candidate <= 2100:
            return candidate
    return None


def _external_ids(raw: dict[str, Any]) -> dict[str, str]:
    """``mal_id`` is the whole point: it is what `recsys.link` joins on."""
    ids: dict[str, str] = {"anilist_id": str(raw["id"])}
    if raw.get("idMal") is not None:
        ids["mal_id"] = str(raw["idMal"])
        ids["mal_url"] = f"https://myanimelist.net/anime/{raw['idMal']}"
    if isinstance(raw.get("siteUrl"), str) and raw["siteUrl"]:
        ids["anilist_url"] = raw["siteUrl"]
    return ids


def map_anilist_media(
    raw: dict[str, Any], *, fetched_at: datetime | None = None, min_tag_rank: int = 60
) -> UnifiedItem | None:
    """Map one AniList media record to a UnifiedItem. None = unusable, skip it."""
    if not isinstance(raw, dict) or raw.get("id") is None:
        return None

    canonical, alt_titles = _titles(raw)
    if canonical is None:
        return None

    score = raw.get("averageScore")
    # AniList scores 0-100; the unified scale is 0-10.
    mean_rating = float(score) / 10.0 if isinstance(score, (int, float)) else None
    if mean_rating is not None and not (0.0 <= mean_rating <= 10.0):
        mean_rating = None

    popularity = raw.get("popularity")

    return UnifiedItem(
        domain=DOMAIN,
        source=SOURCE,
        source_id=str(raw["id"]),
        title=canonical,
        alt_titles=alt_titles,
        year=_year(raw),
        creators=_creators(raw),
        tags=_tags(raw, min_tag_rank),
        description=clean_description(raw.get("description")),
        external_ids=_external_ids(raw),
        # Users with it on a list. A within-source size signal, not comparable
        # to MAL's member count.
        popularity=float(popularity) if isinstance(popularity, (int, float)) else None,
        mean_rating=mean_rating,
        n_ratings=_n_ratings(raw),
        image_url=((raw.get("coverImage") or {}).get("large")) or None,
        fetched_at=fetched_at or utcnow(),
        # Keeps the adaptation edges and the full tag list for later phases, so
        # mining them never costs another request.
        raw_json=raw,
    )


def _batched(seq: list[int], size: int) -> Iterator[list[int]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# --------------------------------------------------------------------------
# ingester
# --------------------------------------------------------------------------


class AniListAnimeIngester(Ingester):
    name = "anime_anilist"
    domain = DOMAIN
    source = SOURCE

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.url = settings.anilist_url
        self.target_count = settings.anilist_target_count  # 0 = every known id
        self.per_page = max(1, min(MAX_PER_PAGE, settings.anilist_page_size))
        self.max_retries = settings.anilist_max_retries
        self.min_tag_rank = settings.anilist_min_tag_rank
        self.limiter = RateLimiter(settings.anilist_rate_per_sec, settings.anilist_rate_per_min)
        self.cache = DiskCache(settings.cache_dir, SOURCE, settings.http_cache_ttl_days)
        self._client: httpx.AsyncClient | None = None

    # ---- what to fetch ----------------------------------------------------

    def mal_ids_to_fetch(self) -> list[int]:
        """The MAL ids already in the catalog, most popular first.

        Read in one pass on its own connection, before the run loop starts
        writing. Holding a second connection open across the write batches would
        put a reader and a writer on the same file for the length of the run;
        WAL tolerates that, but there is no reason to find out.

        Ordering by popularity means an interrupted or ``--limit``ed run
        enriches the titles anyone would actually look at first.
        """
        from recsys.db import session

        with session() as conn:
            rows = conn.execute(
                """SELECT CAST(json_extract(external_ids, '$.mal_id') AS INTEGER) AS mal_id,
                          MAX(popularity) AS pop
                   FROM items
                   WHERE domain = ?
                     AND source != ?
                     AND json_extract(external_ids, '$.mal_id') IS NOT NULL
                   GROUP BY mal_id
                   ORDER BY pop DESC""",
                (DOMAIN, SOURCE),
            ).fetchall()

        ids = [r["mal_id"] for r in rows if r["mal_id"]]
        if self.target_count:
            ids = ids[: self.target_count]
        return ids

    # ---- http -------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            enable_os_truststore()  # must precede the first TLS handshake
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=10.0),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "recsys-loom/0.1 (local)",
                },
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post_batch(self, ids: list[int]) -> list[dict[str, Any]]:
        """One GraphQL request for up to `per_page` anime, cached by id set."""
        # The disk cache is keyed by URL; a GraphQL POST is distinguished by its
        # variables, so they go into the key. Sorted, so the same batch requested
        # in a different order is still a hit.
        cache_key = f"{self.url}#anime#{','.join(str(i) for i in sorted(ids))}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            self.n_cached += 1
            return cached

        client = self._get_client()
        payload = {"query": QUERY, "variables": {"ids": ids, "perPage": self.per_page}}
        last_error = "unknown"

        for attempt in range(self.max_retries):
            await self.limiter.acquire()
            try:
                resp = await client.post(self.url, json=payload)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                delay = backoff_delay(attempt)
                log.warning("%s -- retrying in %.1fs (%d/%d)",
                            last_error, delay, attempt + 1, self.max_retries)
                await asyncio.sleep(delay)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                delay = parse_retry_after(resp.headers.get("Retry-After"))
                if delay is None:
                    delay = backoff_delay(attempt, base=2.0 if resp.status_code == 429 else 1.0)
                last_error = f"HTTP {resp.status_code}"
                log.warning("%s from AniList -- backing off %.1fs (%d/%d)",
                            last_error, delay, attempt + 1, self.max_retries)
                await asyncio.sleep(delay)
                continue

            if resp.status_code != 200:
                raise RuntimeError(f"AniList returned HTTP {resp.status_code}: {resp.text[:200]}")

            body = resp.json()
            # GraphQL reports failures inside a 200. A partial response still
            # carries usable media, so warn and keep whatever came back.
            if body.get("errors"):
                messages = "; ".join(
                    str(e.get("message")) for e in body["errors"][:3] if isinstance(e, dict)
                )
                log.warning("AniList GraphQL errors: %s", messages)
                if not (body.get("data") or {}).get("Page"):
                    raise RuntimeError(f"AniList query failed: {messages}")

            media = ((body.get("data") or {}).get("Page") or {}).get("media") or []
            self.cache.set(cache_key, media)
            return media

        raise RuntimeError(
            f"AniList request failed after {self.max_retries} attempts ({last_error})"
        )

    # ---- Ingester contract ------------------------------------------------

    async def fetch(self) -> AsyncIterator[dict[str, Any]]:
        self._fetched_at = utcnow()

        ids = self.mal_ids_to_fetch()
        if not ids:
            log.warning(
                "anilist: no MAL ids in the catalog -- run anime_mal_csv first; "
                "this ingester enriches the catalog rather than importing one"
            )
            return

        batches = (len(ids) + self.per_page - 1) // self.per_page
        log.info("anilist: %d ids in %d batches of %d", len(ids), batches, self.per_page)

        yielded = 0
        for n, batch in enumerate(_batched(ids, self.per_page), start=1):
            media = await self._post_batch(batch)
            # AniList silently omits ids it does not carry; that is a normal
            # miss (the 2018 dump has entries AniList never had), not an error.
            for record in media:
                yield record
                yielded += 1
            if n % 20 == 0 or n == batches:
                log.info("anilist: batch %d/%d, %d records (%d cache hits)",
                         n, batches, yielded, self.cache.hits)

        log.info("anilist: fetched %d records for %d requested ids", yielded, len(ids))

    def map_item(self, raw: dict[str, Any]) -> UnifiedItem | None:
        return map_anilist_media(
            raw,
            fetched_at=getattr(self, "_fetched_at", None),
            min_tag_rank=self.min_tag_rank,
        )
