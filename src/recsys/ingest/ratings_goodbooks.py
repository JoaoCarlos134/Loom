"""goodbooks-10k user ratings -> the ``ratings`` table.

The second ratings corpus, and the first one reachable without a Kaggle
account: ~6M ratings by 53,424 readers over the same 10,000 books
``book_goodbooks`` catalogs, as one 72 MB CSV beside the catalog files on
GitHub. It gives the collaborative model a second domain to learn from.

A sibling of ``ratings_mal_csv.py`` rather than an ``Ingester``, for the reasons
given there. What differs:

* **The file names books by the dump's own 1..10000 ``book_id``**, not the
  ``goodreads_book_id`` the catalog rows are keyed on. ``books.csv`` carries
  both, so it is read first to translate. A rating whose book is not in it is
  skipped -- it could never point at a catalog row.
* **Goodreads stars are 1-5.** Ratings are doubled onto the 1-10 scale MAL and
  your own ratings use, with the same ``RATING_SCALE`` the catalog applies to
  ``mean_rating``. Per-user centring makes the model indifferent to scale, but
  ``report_held_out_recall``'s "loved" bar of 8 is not -- doubled, it means four
  stars or more.
* **No timestamps.** ``rated_at`` is NULL for every row.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import time
import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import httpx

from recsys.config import Settings
from recsys.db import finish_ingest_run, start_ingest_run, upsert_ratings
from recsys.ingest.base import IngestResult
from recsys.ingest.book_goodbooks import DOMAIN, RATING_SCALE, SOURCE
from recsys.log import get_logger
from recsys.net import enable_os_truststore
from recsys.schemas import make_item_uid, make_user_uid

log = get_logger(__name__)

#: Rows per transaction; same reasoning as ``ratings_mal_csv``.
BATCH_SIZE = 50_000

#: Progress log cadence, in rows read.
LOG_EVERY = 1_000_000

RatingRow = tuple[str, str, float, str | None]


def map_rating(
    user_id: str, book_id: str, rating: str, goodreads_ids: dict[str, str]
) -> RatingRow | None:
    """Map one ratings.csv row to a ratings row, or None to skip it."""
    user_id = user_id.strip()
    goodreads_id = goodreads_ids.get(book_id.strip())
    if not user_id or goodreads_id is None:
        return None

    try:
        stars = float(rating)
    except ValueError:
        return None
    if not (1.0 <= stars <= 5.0):
        return None

    return (
        make_user_uid(SOURCE, user_id),
        make_item_uid(DOMAIN, SOURCE, goodreads_id),
        stars * RATING_SCALE,
        None,
    )


class GoodbooksRatingsLoader:
    """Streams goodbooks-10k's ratings.csv into ``ratings``."""

    name: ClassVar[str] = "book_goodbooks_ratings"
    domain: ClassVar[str] = DOMAIN
    # The ``ingest_runs`` label; kept apart from the catalog's "goodbooks" runs.
    source: ClassVar[str] = "goodbooks_ratings"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.goodbooks_base_url.rstrip("/")
        self.local_dir = settings.goodbooks_path
        self.target_count = settings.goodbooks_ratings_target_count  # 0 = whole file
        self.n_skipped = 0

    def read_text(self, filename: str) -> str:
        """One goodbooks file, from GOODBOOKS_PATH if set or over HTTP."""
        if self.local_dir:
            path = Path(self.local_dir) / filename
            if not path.is_file():
                raise FileNotFoundError(f"GOODBOOKS_PATH has no {filename}: {path}")
            return path.read_text(encoding="utf-8")
        enable_os_truststore()  # must precede the first TLS handshake
        url = f"{self.base_url}/{filename}"
        log.info("downloading %s", url)
        response = httpx.get(url, timeout=300.0, follow_redirects=True)
        response.raise_for_status()
        return response.text

    def goodreads_ids(self) -> dict[str, str]:
        """The dump's ``book_id`` -> the ``goodreads_book_id`` catalog rows use."""
        return {
            row["book_id"]: row["goodreads_book_id"]
            for row in csv.DictReader(io.StringIO(self.read_text("books.csv")))
            if row.get("book_id") and row.get("goodreads_book_id")
        }

    # ---- read -------------------------------------------------------------

    def rows(self) -> Iterator[RatingRow]:
        """Yield mapped rating rows, counting skipped ones as it goes."""
        ids = self.goodreads_ids()
        reader = csv.reader(io.StringIO(self.read_text("ratings.csv")))
        header = next(reader, None)
        if header is None:
            return
        names = ("user_id", "book_id", "rating")
        missing = [name for name in names if name not in header]
        if missing:
            raise ValueError(
                f"ratings.csv is not a goodbooks ratings file: missing {', '.join(missing)}"
            )
        u, b, r = (header.index(name) for name in names)

        read = 0
        for row in reader:
            read += 1
            try:
                mapped = map_rating(row[u], row[b], row[r], ids)
            except IndexError:  # a short / ragged line
                mapped = None
            if mapped is None:
                self.n_skipped += 1
            else:
                yield mapped
            if read % LOG_EVERY == 0:
                log.info("goodbooks_ratings: %d rows read, %d skipped", read, self.n_skipped)
            if self.target_count and read >= self.target_count:
                break

        log.info("goodbooks_ratings: %d rows read, %d skipped", read, self.n_skipped)

    # ---- run loop ---------------------------------------------------------

    async def run(self, conn: sqlite3.Connection) -> IngestResult:
        """Read -> map -> upsert, recording an ``ingest_runs`` row."""
        result = IngestResult(source=self.source, domain=self.domain)
        run_id = start_ingest_run(conn, self.source, self.domain)
        started = time.perf_counter()
        batch: list[RatingRow] = []

        def flush() -> None:
            result.n_inserted += upsert_ratings(conn, batch)
            batch.clear()

        try:
            for row in self.rows():
                result.n_fetched += 1
                batch.append(row)
                if len(batch) >= BATCH_SIZE:
                    flush()
            if batch:
                flush()
        except Exception as exc:
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"
            log.error("%s failed: %s\n%s", self.name, result.error, traceback.format_exc())
            if batch:
                try:
                    flush()
                except Exception:
                    log.exception("%s: could not flush %d buffered rows", self.name, len(batch))
                batch.clear()
        finally:
            result.duration_s = time.perf_counter() - started
            result.n_skipped = self.n_skipped
            finish_ingest_run(
                conn,
                run_id,
                n_fetched=result.n_fetched,
                n_inserted=result.n_inserted,
                status=result.status,
                error=result.error,
            )

        return result
