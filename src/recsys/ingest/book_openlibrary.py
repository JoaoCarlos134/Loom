"""Open Library, to give the book domain the text goodbooks-10k has none of.

``book_goodbooks`` supplies 10,000 books with authors, years, Goodreads ratings
and shelves -- and **not one synopsis**. That left books at 0% description
coverage while films sat at 99%, which matters more here than anywhere else:
books share no identifier with any other domain, so text and tags are the only
way a novel can ever be connected to an anime. This closes that.

**Two phases, because descriptions live one level up.** Open Library separates
*editions* (a printing, addressed by ISBN) from *works* (the book itself), and
only the work carries a description. The batch endpoint resolves up to a hundred
ISBNs to their work keys in a single request; the work then has to be fetched
individually. That is ~100 batch calls plus one request per book, rather than
the 20,000 a naive edition-then-work walk would make.

**This is a second source for the book domain, not a rewrite of the first.**
Rows land under ``book:openlibrary:{isbn}`` beside ``book:goodbooks:{id}``, and
``recsys.link`` collapses them on the shared ISBN -- which is why ``isbn`` is in
``link.LINK_KEYS``. Neither row is complete on its own: Open Library has the
prose and the subject headings, Goodreads has the ratings and the audience size.
That is exactly the case ``links`` exists for.

Keyed by ISBN rather than by Open Library's own work id, deliberately. Two
Goodreads books can resolve to one Open Library work -- different editions of
the same novel -- and keying on the work id would silently collapse them into a
single row that only links back to one of them. The ISBN is what was resolved
and what the link is made on; the OL keys travel in ``external_ids``.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast

import httpx

from recsys.config import Settings
from recsys.ingest.base import Ingester
from recsys.ingest.cache import DiskCache
from recsys.ingest.ratelimit import RateLimiter, backoff_delay, parse_retry_after
from recsys.log import get_logger
from recsys.net import enable_os_truststore
from recsys.schemas import Creator, Domain, UnifiedItem, utcnow

log = get_logger(__name__)

SOURCE = "openlibrary"
DOMAIN = "book"

#: The batch endpoint takes many bibkeys at once. A hundred keeps the URL under
#: any sane length limit while cutting the resolve phase to ~100 requests.
BATCH_SIZE = 100

#: Open Library subject headings run long and repetitive ("Fiction, psychological",
#: "American fiction (fictional works by one author)"). Past this they stop
#: describing the book and start describing the library catalogue.
MAX_SUBJECTS = 25

#: Subject headings that classify a record rather than a story.
_SUBJECT_NOISE = (
    "accessible book", "protected daisy", "in library", "overdrive",
    "large type book", "internet archive wishlist", "reading level",
    "lending library", "open library staff picks", "collection",
    "fictional works by one author", "translations into",
    "bestseller", "award", "book club", "reading group",
)

#: Covers are hot-linked by cover id. The cover service rate-limits lookups by
#: ISBN to 100 per IP every five minutes, and lookups by cover id not at all --
#: so a browser showing a page of forty books must never be sent to the ISBN
#: form. The id is what an ISBN lookup resolves to on their side, and the batch
#: call this ingester already makes returns it for the edition.
COVER_URL = "https://covers.openlibrary.org/b/id/{}-M.jpg"

#: Machine-generated headings: a lowercase namespace, a colon, no space.
#: Open Library carries a lot of these -- ``series:harry_potter``,
#: ``nyt:series_books=2008-03-15`` -- and they are catalogue plumbing. A real
#: heading that happens to contain a colon ("Fiction: general") has a space
#: after it and survives.
_MACHINE_SUBJECT = re.compile(r"^[a-z][a-z0-9_]*:\S")


def is_descriptive(subject: str) -> bool:
    """Whether a subject heading says something about the book."""
    name = subject.strip().lower()
    if not name or len(name) < 3:
        return False
    if _MACHINE_SUBJECT.match(name):
        return False
    return not any(noise in name for noise in _SUBJECT_NOISE)


def clean_description(value: Any) -> str | None:
    """Open Library stores descriptions as a string or as ``{"value": ...}``."""
    if isinstance(value, dict):
        value = value.get("value")
    if not isinstance(value, str):
        return None
    text = value.strip()
    # Many carry a trailing source credit or a bare containing-page link.
    for marker in ("----------", "\r\n\r\n[Source:", "\n\n[Source:", "([source][1])"):
        cut = text.find(marker)
        if cut > 100:
            text = text[:cut]
    return text.strip() or None


def cover_url(*candidates: Any) -> str | None:
    """The first usable cover id among lists of them, as a URL.

    Open Library marks a deleted cover with -1, so only positive integers count.
    The edition's own cover comes first -- it is the printing the ISBN names,
    exactly what the ISBN lookup would show -- then the work's.
    """
    for ids in candidates:
        for cover in ids if isinstance(ids, list) else []:
            if isinstance(cover, int) and not isinstance(cover, bool) and cover > 0:
                return COVER_URL.format(cover)
    return None


def map_openlibrary(
    raw: dict[str, Any], *, fetched_at: Any = None, max_subjects: int = MAX_SUBJECTS
) -> UnifiedItem | None:
    """Map one resolved book to a UnifiedItem. None = nothing worth storing."""
    isbn = (raw.get("_isbn") or "").strip()
    title = (raw.get("title") or "").strip()
    if not isbn or not title:
        return None

    description = clean_description(raw.get("description"))
    subjects = [
        s.strip()
        for s in (raw.get("subjects") or [])
        if isinstance(s, str) and is_descriptive(s)
    ][:max_subjects]

    image_url = cover_url(raw.get("_edition_covers"), raw.get("covers"))

    # A row with no prose, no subjects and no cover adds nothing the goodbooks
    # row does not already have. One with only a cover is kept: it loses the
    # primary election on content and lends the cover to the work.
    if not description and not subjects and image_url is None:
        return None

    creators = [
        Creator(name=a["name"].strip(), role="author")
        for a in (raw.get("_authors") or [])
        if isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"].strip()
    ]

    external_ids = {"isbn": isbn}
    if isinstance(raw.get("key"), str):
        external_ids["openlibrary_work"] = raw["key"].rsplit("/", 1)[-1]
        external_ids["openlibrary_url"] = f"https://openlibrary.org{raw['key']}"

    return UnifiedItem(
        domain=cast(Domain, DOMAIN),
        source=SOURCE,
        source_id=isbn,
        title=title,
        year=None,  # the edition has a date; the work does not
        creators=creators,
        tags=subjects,
        description=description,
        external_ids=external_ids,
        # Open Library carries no audience count and its ratings are sparse.
        # The goodbooks row linked to this one has both; `popularity.py` gives
        # every row of a work the same figure, so nothing is lost by leaving
        # these empty rather than inventing them.
        popularity=None,
        mean_rating=None,
        n_ratings=None,
        image_url=image_url,
        fetched_at=fetched_at or utcnow(),
        raw_json=raw,
    )


def _batched(seq: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


class OpenLibraryIngester(Ingester):
    """Resolves the catalog's ISBNs to Open Library works and takes their text."""

    name = "book_openlibrary"
    domain = DOMAIN
    source = SOURCE

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.url = settings.openlibrary_url.rstrip("/")
        self.target_count = settings.openlibrary_target_count
        self.max_retries = settings.openlibrary_max_retries
        self.max_subjects = settings.openlibrary_max_subjects
        self.limiter = RateLimiter(
            settings.openlibrary_rate_per_sec, settings.openlibrary_rate_per_min
        )
        self.cache = DiskCache(settings.cache_dir, SOURCE, settings.http_cache_ttl_days)
        self._client: httpx.AsyncClient | None = None

    # ---- what to fetch ----------------------------------------------------

    def isbns_to_fetch(self) -> list[str]:
        """ISBNs already in the catalog, most-read books first.

        Read in one pass on its own connection before the run loop starts
        writing, as the other catalog-driven ingesters do.
        """
        from recsys.db import session

        with session() as conn:
            rows = conn.execute(
                """SELECT json_extract(external_ids, '$.isbn') AS isbn
                   FROM items
                   WHERE domain = ?
                     AND source != ?
                     AND json_extract(external_ids, '$.isbn') IS NOT NULL
                   ORDER BY COALESCE(popularity, 0) DESC""",
                (DOMAIN, SOURCE),
            ).fetchall()

        seen: dict[str, None] = {}
        for row in rows:
            isbn = (row["isbn"] or "").strip()
            if isbn:
                seen.setdefault(isbn, None)
        isbns = list(seen)
        return isbns[: self.target_count] if self.target_count else isbns

    # ---- http -------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            enable_os_truststore()
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=10.0),
                # Open Library, like MusicBrainz, wants to know who is calling.
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Loom/0.1 ( https://github.com/JoaoCarlos134/Loom )",
                },
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        key = f"{self.url}{path}?{sorted((params or {}).items())}"
        cached = self.cache.get(key)
        if cached is not None:
            self.n_cached += 1
            return cached

        client = self._get_client()
        last_error = "unknown"
        for attempt in range(self.max_retries):
            await self.limiter.acquire()
            try:
                resp = await client.get(f"{self.url}{path}", params=params)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(backoff_delay(attempt))
                continue

            if resp.status_code == 200:
                payload = resp.json()
                self.cache.set(key, payload)
                return payload
            if resp.status_code == 404:
                return None  # a work Open Library does not have
            if resp.status_code in (429, 503) or resp.status_code >= 500:
                delay = parse_retry_after(resp.headers.get("Retry-After"))
                if delay is None:
                    delay = 2.0 + backoff_delay(attempt, base=4.0)
                await asyncio.sleep(delay)
                last_error = f"HTTP {resp.status_code}"
                continue
            raise RuntimeError(f"Open Library returned HTTP {resp.status_code} for {path}")

        raise RuntimeError(f"Open Library failed after {self.max_retries} attempts "
                           f"({last_error}): {path}")

    # ---- Ingester contract ------------------------------------------------

    async def fetch(self) -> AsyncIterator[dict[str, Any]]:
        self._fetched_at = utcnow()
        isbns = self.isbns_to_fetch()
        if not isbns:
            log.warning("%s: no ISBNs in the catalog -- book_goodbooks has to run "
                        "first; this enriches the books that exist", self.name)
            return

        log.info("%s: resolving %d ISBNs in %d batches",
                 self.name, len(isbns), (len(isbns) + BATCH_SIZE - 1) // BATCH_SIZE)

        # Phase one: many ISBNs to their work keys in one request each.
        work_of: dict[str, str] = {}
        extra: dict[str, dict[str, Any]] = {}
        for batch in _batched(isbns, BATCH_SIZE):
            payload = await self._get_json(
                "/api/books",
                {"bibkeys": ",".join(f"ISBN:{i}" for i in batch),
                 "format": "json", "jscmd": "details"},
            ) or {}
            for bibkey, entry in payload.items():
                isbn = bibkey.split(":", 1)[-1]
                details = (entry or {}).get("details") or {}
                works = details.get("works") or []
                if works and isinstance(works[0], dict) and works[0].get("key"):
                    work_of[isbn] = works[0]["key"]
                    extra[isbn] = {
                        "title": details.get("title"),
                        "_authors": details.get("authors"),
                        # The edition's cover ids, already in this answer.
                        "_edition_covers": details.get("covers") or [],
                    }

        log.info("%s: %d of %d ISBNs resolved to a work", self.name, len(work_of), len(isbns))

        # Phase two: the work itself, which is where the description lives.
        done = 0
        for isbn, key in work_of.items():
            work = await self._get_json(f"{key}.json")
            done += 1
            if not isinstance(work, dict):
                self.n_skipped += 1
                continue
            record = {**work, **extra.get(isbn, {}), "_isbn": isbn}
            # The work's title is the canonical one; fall back to the edition's.
            record["title"] = work.get("title") or extra.get(isbn, {}).get("title")
            yield record
            if done % 500 == 0:
                log.info("%s: %d/%d works fetched (%d cache hits)",
                         self.name, done, len(work_of), self.cache.hits)

        log.info("%s: fetched %d works for %d ISBNs", self.name, done, len(isbns))

    def map_item(self, raw: dict[str, Any]) -> UnifiedItem | None:
        return map_openlibrary(
            raw,
            fetched_at=getattr(self, "_fetched_at", None),
            max_subjects=self.max_subjects,
        )
