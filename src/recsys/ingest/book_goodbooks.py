"""goodbooks-10k for the ``book`` domain.

Ten thousand books with authors, publication years, Goodreads ratings and a
million shelf assignments, as three small CSVs served straight from GitHub. No
credential, no bulk archive, no Kaggle account -- the whole source is about 5 MB
and downloads in seconds.

**Books are the domain that has nothing in common with anything.** Anime and
manga were joined by MAL ids; films carry an IMDb id a future MovieLens source
can join on. A novel and an anime share no identifier anywhere, so if Loom is
ever to connect them it must be through what they are *about*. That is the whole
cross-domain premise, and this is the domain that tests it honestly.

Two things this dump does not have, worth knowing before trusting it:

* **No synopsis.** Same gap the MAL dump had, and the same consequence: FTS and
  any text-based similarity lean on titles alone here. ``book_openlibrary``
  exists to close it.
* **Shelves, not tags.** Goodreads shelves are user-defined and roughly a third
  of the popular ones are library management -- ``to-read``,
  ``currently-reading``, ``books-i-own``. Those say nothing about a book and are
  excluded; what remains (``dystopian``, ``young-adult``, ``historical-fiction``)
  is a real vocabulary.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx

from recsys.config import Settings
from recsys.ingest.base import Ingester
from recsys.log import get_logger
from recsys.net import enable_os_truststore
from recsys.schemas import Creator, Domain, UnifiedItem, utcnow

log = get_logger(__name__)

SOURCE = "goodbooks"
DOMAIN = "book"

FILES = ("books.csv", "book_tags.csv", "tags.csv")

#: Goodreads shelves that describe a reader's filing system rather than the
#: book. Matched as whole shelf names or by substring, because the same idea is
#: spelled a dozen ways (``to-read``, ``to-read-fiction``, ``my-to-read``).
_SHELF_NOISE = (
    "to-read", "currently-reading", "read", "favorite", "favourites", "owned",
    "own", "books-i-own", "my-books", "my-library", "wish-list", "wishlist",
    "default", "kindle", "audiobook", "ebook", "paperback", "hardcover",
    "library", "borrowed", "bought", "shelf", "series", "abandoned", "dnf",
    "all-time", "book-club", "re-read", "reread", "unfinished", "tbr",
)

#: Goodreads scores books 0-5; the unified scale is 0-10.
RATING_SCALE = 2.0


#: Compared with trailing plurals stripped from both sides, so one entry covers
#: `favorite` / `favorites` / `favourites` rather than needing every spelling
#: listed. A denylist still needs maintenance; this at least removes the most
#: common way of missing an entry.
_SHELF_NOISE_STEMS = frozenset(noise.rstrip("s") for noise in _SHELF_NOISE)


def is_descriptive(shelf: str) -> bool:
    """Whether a shelf name says something about the book.

    Deliberately strict. A shelf that survives here becomes a feature the
    cross-domain model matches on, and ``owned`` matching ``owned`` would be a
    similarity signal that means nothing at all.
    """
    name = shelf.strip().lower()
    if not name or name.isdigit() or len(name) < 3:
        return False
    # Junk tags in this dump: "-", "--1-", "--122-".
    if not any(ch.isalpha() for ch in name):
        return False
    if name.rstrip("s") in _SHELF_NOISE_STEMS:
        return False
    # Any component being management vocabulary condemns the whole shelf:
    # "books-i-own", "5-star-read", "read-in-2015".
    parts = {part.rstrip("s") for part in name.replace("_", "-").split("-")}
    return not parts & _SHELF_NOISE_STEMS


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_float(value: Any) -> float | None:
    text = _clean(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def normalise_isbn(value: Any) -> str | None:
    """Zero-pad a 10-digit ISBN back to its proper width.

    The CSV stores ISBNs numerically, so *The Hunger Games* arrives as
    ``439023483`` rather than ``0439023483``. Left alone, every ISBN beginning
    with a zero fails to resolve against Open Library.
    """
    text = _clean(value)
    if text is None:
        return None
    text = text.replace("-", "").replace(" ", "")
    if not text or not text[:-1].isdigit():
        return None
    return text.zfill(10) if len(text) < 10 else text


def _year(value: Any) -> int | None:
    """Publication year, dropped when it falls outside the schema's range.

    This dump carries negative years for antiquity -- Aesop and Homer -- which
    the unified schema rejects rather than silently coerce. Losing the year on a
    handful of ancient texts is better than inventing one.
    """
    number = _as_float(value)
    if number is None:
        return None
    year = int(number)
    return year if 1000 <= year <= 2100 else None


def _cover(value: Any) -> str | None:
    """The cover URL, or ``None`` for Goodreads' "nophoto" placeholder.

    A third of this dump points at that grey silhouette. Stored as a cover it
    is not a gap, so ``fill_from_siblings`` -- which fills only what is
    missing -- could never replace it with a real one from Open Library.
    """
    url = _clean(value)
    if url is None or "/nophoto/" in url:
        return None
    return url


def map_goodbooks_row(
    raw: dict[str, Any], *, tags: list[str] | None = None, fetched_at: Any = None
) -> UnifiedItem | None:
    """Map one books.csv row to a UnifiedItem. None = unusable, skip it."""
    # `goodreads_book_id`, not the dump's own 1..10000 `book_id`: it is the id
    # the wider world uses and the one another Goodreads source would join on.
    book_id = _clean(raw.get("goodreads_book_id"))
    if book_id is None:
        return None

    title = _clean(raw.get("title"))
    original = _clean(raw.get("original_title"))
    canonical = title or original
    if canonical is None:
        return None

    alt_titles = [original] if original and original != canonical else []

    creators = [
        Creator(name=name.strip(), role="author")
        for name in (_clean(raw.get("authors")) or "").split(",")
        if name.strip()
    ]

    rating = _as_float(raw.get("average_rating"))
    mean_rating = rating * RATING_SCALE if rating and 0 < rating <= 5 else None

    count = _as_float(raw.get("ratings_count"))
    n_ratings = int(count) if count and count > 0 else None

    external_ids: dict[str, str] = {"goodreads_book_id": book_id}
    if (work_id := _clean(raw.get("work_id"))) is not None:
        external_ids["goodreads_work_id"] = work_id
    if (isbn := normalise_isbn(raw.get("isbn"))) is not None:
        external_ids["isbn"] = isbn
    external_ids["goodreads_url"] = f"https://www.goodreads.com/book/show/{book_id}"

    return UnifiedItem(
        domain=cast(Domain, DOMAIN),
        source=SOURCE,
        source_id=book_id,
        title=canonical,
        alt_titles=alt_titles,
        year=_year(raw.get("original_publication_year")),
        creators=creators,
        tags=tags or [],
        # This dump has no synopsis at all; `book_openlibrary` supplies one.
        description=None,
        external_ids=external_ids,
        popularity=float(n_ratings) if n_ratings else None,
        mean_rating=mean_rating,
        n_ratings=n_ratings,
        image_url=_cover(raw.get("image_url")),
        fetched_at=fetched_at or utcnow(),
        raw_json=raw,
    )


class GoodbooksIngester(Ingester):
    """Reads the three goodbooks-10k CSVs and joins books to their shelves."""

    name = "book_goodbooks"
    domain = DOMAIN
    source = SOURCE

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.base_url = settings.goodbooks_base_url.rstrip("/")
        self.local_dir = settings.goodbooks_path
        self.target_count = settings.goodbooks_target_count
        self.min_tag_count = settings.goodbooks_min_tag_count
        self.max_tags = settings.goodbooks_max_tags

    # ---- source files -----------------------------------------------------

    def read_csv(self, filename: str) -> list[dict[str, Any]]:
        """Read one CSV, from a local directory if configured or over HTTP.

        The three files total ~5 MB, so they are fetched fresh rather than
        cached -- a cache would be more code than the download it saves.
        """
        if self.local_dir:
            path = Path(self.local_dir) / filename
            if not path.is_file():
                raise FileNotFoundError(f"GOODBOOKS_PATH has no {filename}: {path}")
            text = path.read_text(encoding="utf-8")
        else:
            enable_os_truststore()  # must precede the first TLS handshake
            url = f"{self.base_url}/{filename}"
            log.info("downloading %s", url)
            response = httpx.get(url, timeout=120.0, follow_redirects=True)
            response.raise_for_status()
            text = response.text
        return list(csv.DictReader(io.StringIO(text)))

    def shelves_by_book(self) -> dict[str, list[str]]:
        """goodreads_book_id -> its most-used descriptive shelves."""
        names = {
            row["tag_id"]: row["tag_name"]
            for row in self.read_csv("tags.csv")
            if row.get("tag_id") and row.get("tag_name")
        }

        counted: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for row in self.read_csv("book_tags.csv"):
            shelf = names.get(row.get("tag_id", ""))
            if shelf is None or not is_descriptive(shelf):
                continue
            try:
                count = int(row.get("count") or 0)
            except ValueError:
                continue
            if count >= self.min_tag_count:
                counted[row["goodreads_book_id"]].append((count, shelf))

        return {
            book_id: [shelf for _, shelf in sorted(entries, reverse=True)[: self.max_tags]]
            for book_id, entries in counted.items()
        }

    # ---- Ingester contract ------------------------------------------------

    async def fetch(self) -> AsyncIterator[dict[str, Any]]:
        self._fetched_at = utcnow()
        shelves = self.shelves_by_book()
        log.info("goodbooks: %d books carry descriptive shelves", len(shelves))

        rows = self.read_csv("books.csv")
        for n, row in enumerate(rows, start=1):
            row["_shelves"] = shelves.get(row.get("goodreads_book_id", ""), [])
            yield row
            if self.target_count and n >= self.target_count:
                break

        log.info("goodbooks: read %d books", min(len(rows), self.target_count or len(rows)))

    def map_item(self, raw: dict[str, Any]) -> UnifiedItem | None:
        raw = dict(raw)
        shelves = raw.pop("_shelves", [])
        return map_goodbooks_row(
            raw, tags=shelves, fetched_at=getattr(self, "_fetched_at", None)
        )
