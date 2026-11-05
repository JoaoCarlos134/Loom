"""Wikidata's MyAnimeList <-> TMDB cross-references -> the ``id_map`` table.

TMDB files a large share of anime under television and film, so a full build
held *Steins;Gate* twice: once as ``anime:anilist:9253`` and once as
``tv:tmdb:42509``, two unrelated works as far as the catalog could tell. Rating
one recommended the other. ``link.py`` joins rows on ids the rows themselves
carry, and no TMDB record carries a MyAnimeList id.

Wikidata does. Items for anime series and films carry ``P4086`` (MyAnimeList
anime ID) beside ``P4983`` (TMDB TV series ID) or ``P4947`` (TMDB movie ID),
which is an exact, third-party claim that the two records are one work. One
SPARQL query returns all of them; the data is CC0.

**A sibling contract, not an** ``Ingester``. The output is not a catalog row --
it is a statement about two rows other sources wrote -- so it goes to its own
table through ``db.replace_id_map``, shares ``IngestResult`` and the
``ingest_runs`` row, and satisfies ``IngestRunner`` like the ratings loaders do.
Adding another map later (say, an IMDb one) is the same three-step seam: one
file, one registry line, its settings.

What the linker does with a claim, including when it refuses to act on one,
lives in ``link.py``. This module only states the claims faithfully.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import traceback
from pathlib import Path
from typing import Any, ClassVar

import httpx

from recsys.config import Settings
from recsys.db import IdMapRow, finish_ingest_run, replace_id_map, start_ingest_run
from recsys.ingest.base import IngestResult
from recsys.ingest.cache import DiskCache
from recsys.ingest.ratelimit import backoff_delay, parse_retry_after
from recsys.log import get_logger
from recsys.net import enable_os_truststore

log = get_logger(__name__)

SOURCE = "wikidata"

#: Every item with a MyAnimeList anime id and a TMDB id of either kind. A UNION
#: rather than two OPTIONALs, so each result row carries exactly one TMDB id and
#: an item with neither is never returned at all.
SPARQL = """\
SELECT ?mal ?tv ?movie WHERE {
  ?item wdt:P4086 ?mal .
  { ?item wdt:P4983 ?tv . } UNION { ?item wdt:P4947 ?movie . }
}"""

#: Wikidata's User-Agent policy asks every client to name itself and a way to
#: reach whoever runs it; anonymous agents are throttled or blocked.
USER_AGENT = "Loom/0.1 ( https://github.com/JoaoCarlos134/Loom )"

#: SPARQL variable -> the catalog domain whose TMDB rows it names.
_TMDB_DOMAINS = {"tv": "tv", "movie": "movie"}


def _value(binding: dict[str, Any], name: str) -> str | None:
    cell = binding.get(name)
    if not isinstance(cell, dict):
        return None
    value = cell.get("value")
    if not isinstance(value, str):
        return None
    value = value.strip()
    # Both namespaces are plain integers. Anything else is a malformed claim
    # (Wikidata does not enforce the format), and a malformed id joins nothing.
    return value if value.isdigit() else None


def parse_bindings(payload: Any) -> list[IdMapRow]:
    """SPARQL JSON results -> exact (anime mal_id) <-> (TMDB domain, tmdb_id) claims."""
    bindings = ((payload or {}).get("results") or {}).get("bindings") or []
    rows: set[IdMapRow] = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        mal = _value(binding, "mal")
        if mal is None:
            continue
        for variable, domain in _TMDB_DOMAINS.items():
            tmdb = _value(binding, variable)
            if tmdb is not None:
                rows.add(("anime", "mal_id", mal, domain, "tmdb_id", tmdb))
    return sorted(rows)


class WikidataIdMapLoader:
    """Loads Wikidata's MAL <-> TMDB claims into ``id_map``."""

    name: ClassVar[str] = "idmap_wikidata"
    domain: ClassVar[str] = "anime"
    source: ClassVar[str] = SOURCE

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.url = settings.wikidata_sparql_url
        self.local_path = settings.wikidata_idmap_path
        self.max_retries = settings.wikidata_max_retries
        self.cache = DiskCache(settings.cache_dir, SOURCE, settings.http_cache_ttl_days)
        self.n_cached = 0
        #: Tests hand in an ``httpx.MockTransport``; nothing else sets it.
        self.transport: httpx.AsyncBaseTransport | None = None

    @property
    def _cache_key(self) -> str:
        return f"{self.url}?{SPARQL}"

    async def payload(self) -> Any:
        """The query result: from WIKIDATA_IDMAP_PATH, the cache, or one request.

        Not cached here: ``run`` caches a response only once it has parsed into
        claims, so a malformed or empty answer is never kept for a month.
        """
        if self.local_path:
            path = Path(self.local_path)
            if not path.is_file():
                raise FileNotFoundError(f"WIKIDATA_IDMAP_PATH does not exist: {path}")
            return json.loads(path.read_text(encoding="utf-8"))

        cached = self.cache.get(self._cache_key)
        if cached is not None:
            self.n_cached += 1
            return cached

        enable_os_truststore()
        last_error = "unknown"
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=15.0),
            headers={"Accept": "application/sparql-results+json", "User-Agent": USER_AGENT},
            follow_redirects=True,
            transport=self.transport,
        ) as client:
            for attempt in range(self.max_retries):
                try:
                    resp = await client.get(self.url, params={"query": SPARQL, "format": "json"})
                except httpx.HTTPError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    await asyncio.sleep(backoff_delay(attempt, base=4.0))
                    continue
                if resp.status_code == 200:
                    return resp.json()
                # The query service answers 429 with a Retry-After when a client
                # has used its 60 seconds of processing per minute.
                if resp.status_code == 429 or resp.status_code >= 500:
                    delay = parse_retry_after(resp.headers.get("Retry-After"))
                    await asyncio.sleep(delay if delay is not None
                                        else 5.0 + backoff_delay(attempt, base=4.0))
                    last_error = f"HTTP {resp.status_code}"
                    continue
                raise RuntimeError(f"Wikidata query service returned HTTP {resp.status_code}")
        raise RuntimeError(f"Wikidata query failed after {self.max_retries} attempts "
                           f"({last_error})")

    async def run(self, conn: sqlite3.Connection) -> IngestResult:
        """Fetch -> parse -> replace this source's claims, recording an ingest run.

        An answer that parses to no claims at all is a failed run, and the
        claims already stored are kept. Wikidata carries thousands of these
        pairs; zero means a broken response or a stale file, and replacing the
        map with nothing would silently unlink every twin on the next relink.
        """
        result = IngestResult(source=self.source, domain=self.domain)
        run_id = start_ingest_run(conn, self.source, self.domain)
        started = time.perf_counter()
        try:
            payload = await self.payload()
            bindings = ((payload or {}).get("results") or {}).get("bindings") or []
            rows = parse_bindings(payload)
            result.n_fetched = len(bindings)
            if not rows:
                raise ValueError(
                    f"the answer held {len(bindings)} result rows and no usable claim; "
                    "the stored map was left as it was"
                )
            if not self.local_path and self.n_cached == 0:
                self.cache.set(self._cache_key, payload)
            result.n_inserted = replace_id_map(conn, SOURCE, rows)
            result.n_skipped = len(bindings) - result.n_inserted
            log.info("%s: %d claims from %d result rows", self.name, result.n_inserted,
                     len(bindings))
        except Exception as exc:
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"
            log.error("%s failed: %s\n%s", self.name, result.error, traceback.format_exc())
        finally:
            result.duration_s = time.perf_counter() - started
            result.n_cached = self.n_cached
            finish_ingest_run(
                conn, run_id, n_fetched=result.n_fetched, n_inserted=result.n_inserted,
                n_cached=result.n_cached, status=result.status, error=result.error,
            )
        return result
