"""Shared AniList GraphQL machinery: one client, one mapper, two domains.

AniList serves anime and manga through the same ``Media`` type, the same
endpoint and the same rate limit, so the client, the mapping and the batching
live here and the domain modules stay thin. ``anime_anilist.py`` and
``manga_anilist.py`` differ in exactly three things: which domain the rows land
in, which ``type:`` the query asks for, and where the list of ids to fetch comes
from.

That split is the project invariant taken seriously. Adding manga had to be one
new file plus one registry line; it was not, because the AniList client was
welded to the anime module. Fixing the abstraction was the right move rather
than copying 250 lines of retry-and-cache logic into a second file.

Rate limit: the public endpoint advertised ``x-ratelimit-limit: 30`` per minute
when this was written, and 429s are retried honouring ``Retry-After``. Every
response is cached on disk, keyed by the id batch, so a re-run is offline.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
from datetime import datetime
from typing import Any, AsyncIterator, ClassVar, Iterator

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

#: AniList caps a page at 50, and the id filters accept a list, so one request
#: covers 50 works.
MAX_PER_PAGE = 50

#: The field list is identical for both media types -- ``Media`` is one GraphQL
#: type. Only the filter and ``type:`` are substituted.
_QUERY_TEMPLATE = """
query ($ids: [Int], $perPage: Int) {
  Page(page: 1, perPage: $perPage) {
    media(%(id_field)s: $ids, type: %(media_type)s) {
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
      staff { edges { role node { name { full } } } }
      coverImage { large }
      stats { scoreDistribution { score amount } }
      relations {
        edges { relationType node { id idMal type format title { romaji } } }
      }
    }
  }
}
"""


def build_query(*, id_field: str, media_type: str) -> str:
    """Substitute the two things that vary. Both are class constants, never
    user input, so this cannot become an injection vector."""
    return _QUERY_TEMPLATE % {"id_field": id_field, "media_type": media_type}


# Descriptions arrive with light HTML even at asHtml:false, and often end with a
# sourcing note that is noise for text similarity.
_TAG_RE = re.compile(r"<[^>]+>")
_SOURCE_NOTE = re.compile(r"\s*\((?:Source|Note)\s*:[^)]*\)\s*$", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t]*\n[ \t]*")


# --------------------------------------------------------------------------
# mapping (pure -- unit-tested against saved fixtures, no network)
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
    # Matches the other mappers: the format is a useful cross-domain facet.
    # TV / MOVIE / OVA for anime, MANGA / NOVEL / ONE_SHOT for manga.
    if isinstance(raw.get("format"), str) and raw["format"].strip():
        out.append(raw["format"].strip())
    return out


#: Credits that belong to an *edition*, not to the work. AniList lists a
#: manga's translators, letterers and typesetters per language, which is a lot
#: of names: excluding them keeps "same person" from bridging two unrelated
#: manga because they share a Spanish translator.
_EDITION_ROLE = re.compile(r"translat|letter|editing|editor|proofread|typeset", re.I)

#: Ordering for the credits that remain. AniList returns staff in no
#: particular order, so without this the first creator shown for *Shingeki no
#: Kyojin* is an assistant rather than Hajime Isayama.
_ROLE_RANK = (
    (re.compile(r"story|original creator|author|creator", re.I), 0),
    (re.compile(r"\bart\b|illustration|design", re.I), 1),
)


def _staff_rank(role: str | None) -> int:
    for pattern, rank in _ROLE_RANK:
        if role and pattern.search(role):
            return rank
    return 2


def _creators(raw: dict[str, Any]) -> list[Creator]:
    """Studios for anime, credited staff for manga.

    Manga carry no studio, so without staff a manga row would have no creator
    at all -- and "same author" is one of the three cross-domain bridges. An
    anime's ``Original Creator`` credit is that bridge in its purest form: it
    names the person whose manga the series adapts.
    """
    out: list[Creator] = []
    for edge in ((raw.get("studios") or {}).get("edges") or []):
        if not isinstance(edge, dict):
            continue
        name = ((edge.get("node") or {}).get("name") or "").strip()
        if name:
            out.append(Creator(name=name, role="studio" if edge.get("isMain") else "producer"))

    seen = {c.name for c in out}
    staff: list[tuple[int, int, Creator]] = []
    for order, edge in enumerate((raw.get("staff") or {}).get("edges") or []):
        if not isinstance(edge, dict):
            continue
        name = (((edge.get("node") or {}).get("name") or {}).get("full") or "").strip()
        role = (edge.get("role") or "").strip() or None
        if not name or name in seen or (role and _EDITION_ROLE.search(role)):
            continue
        seen.add(name)
        # `order` keeps the sort stable within a rank rather than by name.
        staff.append((_staff_rank(role), order, Creator(name=name, role=role)))

    out.extend(creator for _, _, creator in sorted(staff, key=lambda t: (t[0], t[1])))
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


def _external_ids(raw: dict[str, Any], domain: str) -> dict[str, str]:
    """``mal_id`` is what `recsys.link` joins on within a domain.

    The MAL url is domain-specific: MyAnimeList keeps anime and manga in
    separate id spaces behind separate paths, and pointing a manga id at
    /anime/ would produce a confidently wrong link.
    """
    ids: dict[str, str] = {"anilist_id": str(raw["id"])}
    if raw.get("idMal") is not None:
        ids["mal_id"] = str(raw["idMal"])
        path = "manga" if domain == "manga" else "anime"
        ids["mal_url"] = f"https://myanimelist.net/{path}/{raw['idMal']}"
    if isinstance(raw.get("siteUrl"), str) and raw["siteUrl"]:
        ids["anilist_url"] = raw["siteUrl"]
    return ids


def map_anilist_media(
    raw: dict[str, Any],
    domain: str,
    *,
    fetched_at: datetime | None = None,
    min_tag_rank: int = 60,
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
        domain=domain,
        source=SOURCE,
        source_id=str(raw["id"]),
        title=canonical,
        alt_titles=alt_titles,
        year=_year(raw),
        creators=_creators(raw),
        tags=_tags(raw, min_tag_rank),
        description=clean_description(raw.get("description")),
        external_ids=_external_ids(raw, domain),
        # Users with it on a list. A within-source size signal; `popularity.py`
        # is what makes it comparable to another source's count.
        popularity=float(popularity) if isinstance(popularity, (int, float)) else None,
        mean_rating=mean_rating,
        n_ratings=_n_ratings(raw),
        image_url=((raw.get("coverImage") or {}).get("large")) or None,
        fetched_at=fetched_at or utcnow(),
        # Keeps the relation edges and the full tag list for later phases, so
        # mining them never costs another request.
        raw_json=raw,
    )


def _batched(seq: list[int], size: int) -> Iterator[list[int]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# --------------------------------------------------------------------------
# ingester base
# --------------------------------------------------------------------------


class AniListIngester(Ingester):
    """Batched GraphQL fetching. Subclasses supply the domain and the ids."""

    source = SOURCE

    #: "ANIME" or "MANGA".
    media_type: ClassVar[str]
    #: Which AniList filter the ids are passed to -- "idMal_in" or "id_in".
    id_field: ClassVar[str]

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.url = settings.anilist_url
        self.target_count = settings.anilist_target_count  # 0 = every known id
        self.per_page = max(1, min(MAX_PER_PAGE, settings.anilist_page_size))
        self.max_retries = settings.anilist_max_retries
        self.min_tag_rank = settings.anilist_min_tag_rank
        self.limiter = RateLimiter(settings.anilist_rate_per_sec, settings.anilist_rate_per_min)
        self.cache = DiskCache(settings.cache_dir, SOURCE, settings.http_cache_ttl_days)
        self.query = build_query(id_field=self.id_field, media_type=self.media_type)
        # Cached responses are only valid for the query that produced them.
        # Without this, adding a field to the query returns payloads that
        # silently lack it until the 30-day TTL expires -- which is exactly
        # what happened when `staff` was added for the manga domain.
        self.query_fingerprint = hashlib.sha256(self.query.encode("utf-8")).hexdigest()[:12]
        self._client: httpx.AsyncClient | None = None

    # ---- subclass contract ------------------------------------------------

    def ids_to_fetch(self) -> list[int]:
        """The ids this ingester should request, most useful first.

        Read in one pass on its own connection, before the run loop starts
        writing -- holding a reader open across the write batches would put a
        reader and a writer on the same file for the length of the run. WAL
        tolerates that, but there is no reason to find out.
        """
        raise NotImplementedError

    def _limited(self, ids: list[int]) -> list[int]:
        return ids[: self.target_count] if self.target_count else ids

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
        """One GraphQL request for up to `per_page` works, cached by id set."""
        # The disk cache is keyed by URL; a GraphQL POST is distinguished by its
        # variables and its query, so both go into the key -- along with the
        # media type, or anime and manga batches would collide on shared ids.
        # Ids are sorted, so the same batch in a different order is still a hit.
        cache_key = (
            f"{self.url}#{self.media_type}#{self.query_fingerprint}"
            f"#{','.join(str(i) for i in sorted(ids))}"
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            self.n_cached += 1
            return cached

        client = self._get_client()
        payload = {"query": self.query, "variables": {"ids": ids, "perPage": self.per_page}}
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
                raise RuntimeError(
                    f"AniList returned HTTP {resp.status_code}: {resp.text[:200]}"
                )

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

        ids = self.ids_to_fetch()
        if not ids:
            log.warning("%s: nothing to fetch -- see this ingester's docstring for "
                        "what has to run before it", self.name)
            return

        batches = (len(ids) + self.per_page - 1) // self.per_page
        log.info("%s: %d ids in %d batches of %d", self.name, len(ids), batches, self.per_page)

        yielded = 0
        for n, batch in enumerate(_batched(ids, self.per_page), start=1):
            media = await self._post_batch(batch)
            # AniList silently omits ids it does not carry; that is a normal
            # miss, not an error.
            for record in media:
                yield record
                yielded += 1
            if n % 20 == 0 or n == batches:
                log.info("%s: batch %d/%d, %d records (%d cache hits)",
                         self.name, n, batches, yielded, self.cache.hits)

        log.info("%s: fetched %d records for %d requested ids", self.name, yielded, len(ids))

    def map_item(self, raw: dict[str, Any]) -> UnifiedItem | None:
        return map_anilist_media(
            raw,
            self.domain,
            fetched_at=getattr(self, "_fetched_at", None),
            min_tag_rank=self.min_tag_rank,
        )
