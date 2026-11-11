"""Wikimedia Commons, to give the music domain a face.

``music_musicbrainz`` stores 6,556 artists and not one picture: MusicBrainz
carries no imagery at all, so the whole domain rendered as grey tiles. This
closes that from the one source whose pictures are free to show.

**Why Commons and not Deezer.** Deezer's artist photos were the easy answer --
4,501 artists already carry a ``deezer_id`` and the image URL is a function of
it, no request needed. They are also label promotional photographs, licensed to
Deezer. Wikidata's ``image`` (P18) points at a file on Wikimedia Commons, which
only hosts media under a free licence or in the public domain; the file page
names the author and the licence, and that page travels with the row as
``image_credit_url`` so the credit is one click from the picture.

**How it is asked.** Wikidata records each artist's MusicBrainz id (P434), so
the join is by the id the catalog already holds -- never by name, which is the
one weak joint ``music_musicbrainz`` has and this does not inherit. One SPARQL
query resolves two hundred artists, so the whole domain is ~33 requests.

**The same shape as ``book_openlibrary``**: a second pass over a domain already
ingested, driven by ids in the catalog, supplying the one field the first
source could not. Rows land at ``music:commons:{mbid}`` beside
``music:musicbrainz:{mbid}``, ``link.py`` joins them on ``musicbrainz_id``, and
``db.fill_from_siblings`` gives the artist the portrait.

**A portrait row must never represent the artist.** It carries no tags and no
audience figure, so ``link._richness`` ranks it below the MusicBrainz row on
content -- and where that row is just as bare, the last tiebreak is the uid, on
which ``music:commons:`` sorts below ``music:musicbrainz:``. That ordering is
load-bearing and ``tests/test_covers.py`` pins it: the embedding matrix
holds representatives only, so a portrait winning the election would silently
drop the artist out of every recommendation until the matrix was rebuilt.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

from recsys.config import Settings
from recsys.ingest.base import Ingester
from recsys.ingest.cache import DiskCache
from recsys.ingest.ratelimit import RateLimiter, backoff_delay, parse_retry_after
from recsys.log import get_logger
from recsys.net import enable_os_truststore
from recsys.schemas import Domain, UnifiedItem, utcnow

log = get_logger(__name__)

SOURCE = "commons"
DOMAIN = "music"

#: Artists per SPARQL query. The ids go in a VALUES clause, so this is a bound
#: on query text (~8 kB at 200), not on what the endpoint will answer.
BATCH_SIZE = 200

_FILE_PATH = "/wiki/Special:FilePath/"

_QUERY = """SELECT ?mbid ?item ?image WHERE {{
  VALUES ?mbid {{ {values} }}
  ?item wdt:P434 ?mbid ; wdt:P18 ?image .
}}"""


def commons_urls(image: str, width: int) -> tuple[str, str] | None:
    """(thumbnail URL, file page URL) for a Wikidata image value, or None.

    Wikidata hands the image back as ``Special:FilePath/<name>``, which is a
    redirect to the original upload -- routinely a 6 MB photograph. The same
    URL takes a ``width`` and answers with a thumbnail of that size instead.
    """
    parts = urlsplit(image)
    if parts.netloc != "commons.wikimedia.org" or not parts.path.startswith(_FILE_PATH):
        return None
    name = parts.path[len(_FILE_PATH):]
    if not name:
        return None
    return (
        f"https://commons.wikimedia.org{_FILE_PATH}{name}?width={width}",
        f"https://commons.wikimedia.org/wiki/File:{name}",
    )


def map_commons(
    raw: dict[str, Any], *, width: int = 500, fetched_at: Any = None
) -> UnifiedItem | None:
    """Map one resolved artist to its portrait row. None = no usable image."""
    mbid = (raw.get("_mbid") or "").strip()
    name = (raw.get("_name") or "").strip()
    urls = commons_urls(raw.get("image") or "", width)
    if not mbid or not name or urls is None:
        return None
    image_url, credit_url = urls

    external_ids = {"musicbrainz_id": mbid, "image_credit_url": credit_url}
    entity = (raw.get("item") or "").rsplit("/", 1)[-1]
    if entity.startswith("Q"):
        external_ids["wikidata_id"] = entity
        external_ids["wikidata_url"] = f"https://www.wikidata.org/wiki/{entity}"

    return UnifiedItem(
        domain=cast(Domain, DOMAIN),
        source=SOURCE,
        source_id=mbid,
        title=name,
        external_ids=external_ids,
        # Nothing else, on purpose. Every field left empty here is one the
        # MusicBrainz row keeps the say on -- see the module docstring.
        image_url=image_url,
        fetched_at=fetched_at or utcnow(),
        raw_json=raw,
    )


class CommonsMusicIngester(Ingester):
    """Resolves the catalog's artists to their Wikimedia Commons portraits."""

    name = "music_commons"
    domain = DOMAIN
    source = SOURCE

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.url = settings.wikidata_sparql_url
        # Wikimedia asks automated clients to say who they are, as MusicBrainz
        # does; the same contact setting answers both.
        self.user_agent = f"Loom/0.1 ( {settings.musicbrainz_contact} )"
        self.width = settings.commons_image_width
        self.target_count = settings.music_commons_target_count
        self.max_retries = settings.wikidata_max_retries
        self.limiter = RateLimiter(settings.wikidata_rate_per_sec, 0.0)
        self.cache = DiskCache(settings.cache_dir, SOURCE, settings.http_cache_ttl_days)
        self._client: httpx.AsyncClient | None = None

    # ---- what to fetch ----------------------------------------------------

    def artists_in_catalog(self) -> list[tuple[str, str]]:
        """(mbid, name) for every artist another source carries, best-known first."""
        from recsys.db import session

        with session() as conn:
            rows = conn.execute(
                """SELECT json_extract(external_ids, '$.musicbrainz_id') AS mbid, title
                     FROM items
                    WHERE domain = ? AND source != ?
                      AND json_extract(external_ids, '$.musicbrainz_id') IS NOT NULL
                    ORDER BY COALESCE(popularity, 0) DESC, title ASC""",
                (DOMAIN, SOURCE),
            ).fetchall()

        seen: dict[str, str] = {}
        for row in rows:
            mbid = (row["mbid"] or "").strip()
            # An MBID is a UUID. It goes into query text below, so anything
            # that is not shaped like one is dropped rather than quoted.
            if len(mbid) == 36 and all(c in "0123456789abcdef-" for c in mbid):
                seen.setdefault(mbid, row["title"])
        pairs = list(seen.items())
        return pairs[: self.target_count] if self.target_count else pairs

    # ---- http -------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            enable_os_truststore()
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(90.0, connect=10.0),
                headers={
                    "Accept": "application/sparql-results+json",
                    "User-Agent": self.user_agent,
                },
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _images(self, mbids: list[str]) -> list[dict[str, Any]]:
        """The (mbid, entity, image) bindings Wikidata has for a batch of MBIDs."""
        key = f"{self.url}?mbids={','.join(mbids)}"
        cached = self.cache.get(key)
        if cached is not None:
            self.n_cached += 1
            return cast(list[dict[str, Any]], cached)

        query = _QUERY.format(values=" ".join(f'"{m}"' for m in mbids))
        client = self._get_client()
        last_error = "unknown"
        for attempt in range(self.max_retries):
            await self.limiter.acquire()
            try:
                # POST: two hundred UUIDs do not fit comfortably in a URL.
                resp = await client.post(self.url, data={"query": query})
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(backoff_delay(attempt))
                continue

            if resp.status_code == 200:
                bindings = resp.json().get("results", {}).get("bindings", [])
                self.cache.set(key, bindings)
                return cast(list[dict[str, Any]], bindings)
            if resp.status_code in (429, 503) or resp.status_code >= 500:
                delay = parse_retry_after(resp.headers.get("Retry-After"))
                if delay is None:
                    delay = 2.0 + backoff_delay(attempt, base=4.0)
                await asyncio.sleep(delay)
                last_error = f"HTTP {resp.status_code}"
                continue
            raise RuntimeError(f"Wikidata returned HTTP {resp.status_code}")

        raise RuntimeError(f"Wikidata failed after {self.max_retries} attempts ({last_error})")

    # ---- Ingester contract ------------------------------------------------

    async def fetch(self) -> AsyncIterator[dict[str, Any]]:
        self._fetched_at = utcnow()
        artists = self.artists_in_catalog()
        if not artists:
            log.warning("%s: no artists in the catalog -- music_musicbrainz has to "
                        "run first; this only adds portraits to them", self.name)
            return

        names = dict(artists)
        mbids = list(names)
        log.info("%s: asking Wikidata about %d artists in %d queries",
                 self.name, len(mbids), (len(mbids) + BATCH_SIZE - 1) // BATCH_SIZE)

        found = 0
        for start in range(0, len(mbids), BATCH_SIZE):
            best: dict[str, dict[str, Any]] = {}
            for binding in await self._images(mbids[start : start + BATCH_SIZE]):
                mbid = (binding.get("mbid") or {}).get("value")
                image = (binding.get("image") or {}).get("value")
                if mbid not in names or not image:
                    continue
                # An artist can have several images and, rarely, several
                # entities. Taking the smallest makes a re-run pick the same one.
                record = {
                    "_mbid": mbid,
                    "_name": names[mbid],
                    "item": (binding.get("item") or {}).get("value"),
                    "image": image,
                }
                if mbid not in best or (image, record["item"] or "") < (
                    best[mbid]["image"], best[mbid]["item"] or ""
                ):
                    best[mbid] = record
            for record in best.values():
                found += 1
                yield record

        log.info("%s: %d of %d artists have a Commons image", self.name, found, len(mbids))

    def map_item(self, raw: dict[str, Any]) -> UnifiedItem | None:
        return map_commons(
            raw, width=self.width, fetched_at=getattr(self, "_fetched_at", None)
        )
