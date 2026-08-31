"""MyAnimeList user ratings (Kaggle: azathoth42/myanimelist) -> the ``ratings`` table.

The first loader for **non-item** data, and deliberately *not* an
:class:`~recsys.ingest.base.Ingester` subclass. That contract is
fetch -> ``UnifiedItem`` -> ``upsert_items``, and every part of it is wrong
here: a rating is not a catalog row, it goes to a different table, and there
are ~19M of them rather than ~14.5k. Forcing both into one base class would
mean parameterising the run loop over row type, upsert function and dedup
strategy -- an abstraction over two implementations that share only their
bookkeeping.

So this is a *sibling* contract instead. It shares what is genuinely shared --
``IngestResult`` and the ``ingest_runs`` row -- and duplicates the ~20-line
batching loop, which is the cheaper of the two costs. It stays runnable from
``bootstrap.py`` structurally: same ``name`` / ``domain`` / ``source`` /
``target_count`` attributes, same ``await run(conn) -> IngestResult``. See
``IngestRunner`` in ``ingest/base.py``.

What lands in the table and what does not:

* **Only rows with a real score.** ``my_score`` is 0 for the ~40% of list
  entries that are plan-to-watch or simply unrated. 0 means "no rating", not
  "rated zero" -- the same call ``anime_mal_csv._parse_score`` already makes
  for the catalog.
* **Score, user, item, timestamp. Nothing else.** ``my_status``,
  ``my_watched_episodes`` and ``my_rewatching`` are implicit-feedback signals
  with no consumer yet; they stay in the file until something needs them.
* **Ratings are not checked against ``items``.** There is no foreign key, so
  this loader and ``anime_mal_csv`` can run in either order, and a rating for
  an anime missing from the catalog survives rather than being silently
  dropped.
"""

from __future__ import annotations

import csv
import sqlite3
import time
import traceback
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from recsys.config import Settings
from recsys.db import finish_ingest_run, start_ingest_run, upsert_ratings
from recsys.ingest.base import IngestResult
from recsys.ingest.kaggle import resolve_kaggle_file
from recsys.log import get_logger
from recsys.schemas import make_item_uid, make_user_uid

log = get_logger(__name__)

DOMAIN = "anime"
#: The MyAnimeList entity namespace, shared with ``anime_mal_csv`` so that a
#: rating's ``item_uid`` actually points at the catalog row it rates.
SOURCE = "mal"

#: Rows per transaction. Far larger than the catalog's 200: these rows are tiny
#: and there are ~19M of them, so per-transaction overhead dominates.
BATCH_SIZE = 50_000

#: Progress log cadence, in rows read.
LOG_EVERY = 1_000_000

RatingRow = tuple[str, str, float, str | None]


#: Every spelling of "no timestamp" this dataset uses. The raw file writes a
#: zeroed MySQL date or a bare ``0``; cleaning turns both into the unix epoch,
#: so ``animelists_cleaned.csv`` carries ~0.5% of rows stamped 1970-01-01.
#: Left alone that reads as a real date and lands in every time-ordered split
#: Phase 3 makes. MAL did not exist in 1970, so the sentinel is unambiguous.
_NULL_STAMPS = ("0000-00-00", "1970-01-01 00:00:00", "1970-01-01T00:00:00")


def _parse_rated_at(value: str) -> str | None:
    """Normalise ``my_last_updated`` to an ISO-8601 string, or None.

    Two shapes occur across this dataset's files: ``animelists_cleaned.csv``
    stores ``"2013-03-03 10:52:53"``, while the raw ``UserAnimeList.csv``
    stores the same instant as a unix epoch. Both are accepted because
    ``MAL_RATINGS_FILENAME`` may point at either.
    """
    text = value.strip()
    if not text or text.startswith(_NULL_STAMPS):
        return None
    if text.isdigit():
        epoch = int(text)
        if epoch <= 0:
            return None
        return datetime.fromtimestamp(epoch, tz=UTC).isoformat()
    return text.replace(" ", "T", 1)


def map_rating(
    username: str, anime_id: str, my_score: str, my_last_updated: str
) -> RatingRow | None:
    """Map one list entry to a ratings row, or None to skip it.

    Takes four strings rather than a row dict on purpose: this runs ~31M times,
    and ``csv.DictReader``'s per-row dict is a measurable share of the runtime.
    """
    username = username.strip()
    anime_id = anime_id.strip()
    if not username or not anime_id:
        return None

    try:
        score = float(my_score)
    except ValueError:
        return None
    # 0 = on the list but unrated. Outside 1-10 = corrupt row.
    if not (1.0 <= score <= 10.0):
        return None

    return (
        make_user_uid(SOURCE, username),
        make_item_uid(DOMAIN, SOURCE, anime_id),
        score,
        _parse_rated_at(my_last_updated),
    )


class MalRatingsLoader:
    """Streams the user-list table of the Kaggle MyAnimeList dump into ``ratings``."""

    name: ClassVar[str] = "anime_mal_ratings"
    domain: ClassVar[str] = DOMAIN
    # The ``ingest_runs`` label. Deliberately not SOURCE: ``anime_mal_csv``
    # already records catalog runs under "mal", and a ratings run reporting
    # millions of rows against that same label would be unreadable.
    source: ClassVar[str] = "mal_ratings"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.target_count = settings.mal_ratings_target_count  # 0 = the whole file
        self.dataset = settings.mal_csv_dataset  # same archive as the anime table
        self.filename = settings.mal_ratings_filename
        self.explicit_path = settings.mal_ratings_path
        self.n_skipped = 0

    def resolve_path(self) -> Path:
        return resolve_kaggle_file(
            dataset=self.dataset,
            filename=self.filename,
            explicit_path=self.explicit_path,
            setting_name="MAL_RATINGS_PATH",
        )

    # ---- read -------------------------------------------------------------

    def rows(self) -> Iterator[RatingRow]:
        """Yield mapped rating rows, counting skipped ones as it goes."""
        path = self.resolve_path()
        log.info("reading %s", path)

        read = 0
        # encoding is explicit because Python on Windows would otherwise open
        # this UTF-8 file as cp1252 and die on the first non-ASCII username.
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header is None:
                return
            try:
                u, a, s, t = (
                    header.index(name)
                    for name in ("username", "anime_id", "my_score", "my_last_updated")
                )
            except ValueError as exc:
                raise ValueError(
                    f"{path.name} is not a MAL user-list file: {exc}"
                ) from None

            for row in reader:
                read += 1
                try:
                    mapped = map_rating(row[u], row[a], row[s], row[t])
                except IndexError:  # a short / ragged line
                    mapped = None
                if mapped is None:
                    self.n_skipped += 1
                else:
                    yield mapped
                if read % LOG_EVERY == 0:
                    log.info("mal_ratings: %d rows read, %d skipped", read, self.n_skipped)
                if self.target_count and read >= self.target_count:
                    break

        log.info("mal_ratings: %d rows read, %d skipped", read, self.n_skipped)

    # ---- run loop ---------------------------------------------------------

    async def run(self, conn: sqlite3.Connection) -> IngestResult:
        """Read -> map -> upsert, recording an ``ingest_runs`` row.

        Nothing here awaits, but the method is a coroutine so that bootstrap can
        drive item ingesters and this loader through one call.
        """
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
            # Same reasoning as ingest/base.py: a failure part-way through a
            # multi-GB file must not discard the rows already read and mapped.
            if batch:
                try:
                    flush()
                except Exception:
                    log.exception(
                        "%s: could not flush %d buffered rows", self.name, len(batch)
                    )
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
