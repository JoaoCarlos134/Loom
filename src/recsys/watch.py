"""Where a work can be watched, in the configured region.

Nothing in the catalog says where a work streams, and it changes too often to
store, so this is answered at read time:

* **A link, always.** TMDB keeps a watch page for every film and show, per
  region, built from JustWatch's data. Any row of the work that is a TMDB
  entry gives one -- an anime reaches its own TMDB entry through ``links``,
  which is the Wikidata id map's work. Failing that, an anime's or manga's
  AniList page (which lists legal streams) or MAL page.
* **The services, when TMDB can be asked.** ``/{movie|tv}/{id}/watch/providers``
  names them per region. It needs the TMDB credential, so ``fetch`` is a
  parameter: ``None`` when there is none, and in tests a fake.

TMDB's terms ask that this data be credited to JustWatch; ``credit`` says so
and the UI prints it.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Any

from recsys.config import Settings
from recsys.link import work_members
from recsys.log import get_logger
from recsys.schemas import WatchInfo, WatchProvider

log = get_logger(__name__)

#: ``(media, tmdb_id) -> TMDB's watch/providers response``.
Fetch = Callable[[str, int], dict[str, Any]]

LOGO_BASE = "https://image.tmdb.org/t/p/w92"

#: TMDB's offer types, cheapest first, and what the UI calls them. A service
#: offering a work two ways is listed once, the cheaper way.
KINDS: tuple[tuple[str, str], ...] = (
    ("flatrate", "stream"), ("free", "free"), ("ads", "ads"), ("rent", "rent"), ("buy", "buy"),
)

#: Domains a work can be *watched* in. Manga is read, but its AniList page
#: lists the legal places to read it, which is the same question.
WATCHABLE = frozenset({"anime", "manga", "movie", "tv"})


def _tmdb_target(conn: sqlite3.Connection, item_uid: str) -> tuple[str, int] | None:
    """(``movie``|``tv``, id) of the first TMDB row describing the work."""
    for uid in work_members(conn, item_uid):
        domain, source, source_id = uid.split(":", 2)
        if source == "tmdb" and source_id.isdigit():
            return ("tv" if domain == "tv" else "movie", int(source_id))
    return None


def _page_link(conn: sqlite3.Connection, item_uid: str) -> str | None:
    """An AniList page, else a MAL one, from any row of the work."""
    import json

    found: dict[str, str] = {}
    for uid in work_members(conn, item_uid):
        row = conn.execute("SELECT external_ids FROM items WHERE item_uid = ?", (uid,)).fetchone()
        ids = json.loads(row[0] or "{}") if row else {}
        for key in ("anilist_url", "mal_url"):
            if isinstance(ids.get(key), str):
                found.setdefault(key, ids[key])
    return found.get("anilist_url") or found.get("mal_url")


def _providers(data: dict[str, Any], region: str) -> tuple[list[WatchProvider], str | None]:
    here = (data.get("results") or {}).get(region) or {}
    out: list[WatchProvider] = []
    seen: set[str] = set()
    for key, kind in KINDS:
        for entry in here.get(key) or []:
            name = entry.get("provider_name")
            if not isinstance(name, str) or name in seen:
                continue
            seen.add(name)
            logo = entry.get("logo_path")
            out.append(WatchProvider(
                name=name, kind=kind,
                logo_url=f"{LOGO_BASE}{logo}" if isinstance(logo, str) else None,
            ))
    link = here.get("link")
    return out, link if isinstance(link, str) else None


def where_to_watch(
    conn: sqlite3.Connection, item_uid: str, *, region: str, fetch: Fetch | None
) -> WatchInfo:
    """Where ``item_uid``'s work can be watched in ``region``."""
    domain = item_uid.split(":", 1)[0]
    info = WatchInfo(region=region)
    if domain not in WATCHABLE:
        return info
    target = _tmdb_target(conn, item_uid)
    if target is None:
        info.link = _page_link(conn, item_uid)
        return info
    media, tmdb_id = target
    info.link = f"https://www.themoviedb.org/{media}/{tmdb_id}/watch?locale={region}"
    if fetch is None:
        return info
    try:
        providers, link = _providers(fetch(media, tmdb_id), region)
    except Exception as exc:  # a lookup that fails still leaves the link
        log.warning("watch providers for %s %s: %s", media, tmdb_id, exc)
        return info
    info.providers = providers
    info.link = link or info.link
    info.credit = "JustWatch"
    return info


class TmdbWatchFetch:
    """The live lookup, kept in memory for ``ttl`` seconds per title.

    Availability changes weekly, not by the minute, and a detail view opened
    twice should not ask twice. Sync on purpose: it runs in FastAPI's thread
    pool, and it is one request.
    """

    def __init__(self, settings: Settings, ttl: float = 12 * 3600) -> None:
        self.base_url = settings.tmdb_base_url.rstrip("/")
        self.read_token = settings.tmdb_read_token
        self.api_key = settings.tmdb_api_key
        self.ttl = ttl
        self._cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def __call__(self, media: str, tmdb_id: int) -> dict[str, Any]:
        key = (media, tmdb_id)
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < self.ttl:
            return hit[1]

        import httpx

        from recsys.net import enable_os_truststore

        enable_os_truststore()
        headers = {"Accept": "application/json"}
        params: dict[str, str] = {}
        # The v4 token goes in a header so it never reaches a URL; the v3 key
        # can only be a query parameter, and is never logged.
        if self.read_token:
            headers["Authorization"] = f"Bearer {self.read_token}"
        else:
            params["api_key"] = self.api_key
        r = httpx.get(f"{self.base_url}/{media}/{tmdb_id}/watch/providers",
                      headers=headers, params=params, timeout=8.0)
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        with self._lock:
            self._cache[key] = (time.monotonic(), data)
        return data
