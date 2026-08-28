"""The Ingester contract.

Every domain/source pair is one subclass. A subclass supplies exactly two
things -- how to pull raw records (:meth:`Ingester.fetch`) and how to turn one
raw record into a :class:`~recsys.schemas.UnifiedItem`
(:meth:`Ingester.map_item`). The generic run loop below owns everything else:
batching, upserting, run bookkeeping and error handling.

That split is the whole point of Phase 0. Adding books/movies/music in a later
phase means adding a file and a registry entry -- no edits to db.py, routes.py,
bootstrap.py or the UI.
"""

from __future__ import annotations

import time
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, ClassVar

import sqlite3

from recsys.config import Settings
from recsys.db import finish_ingest_run, start_ingest_run, upsert_items
from recsys.log import get_logger
from recsys.schemas import UnifiedItem

log = get_logger(__name__)

# Rows buffered before each SQLite transaction. Large enough to amortise the
# transaction, small enough that a crash mid-ingest loses little work.
BATCH_SIZE = 200


@dataclass
class IngestResult:
    source: str
    domain: str
    n_fetched: int = 0
    n_inserted: int = 0
    n_updated: int = 0
    n_cached: int = 0
    n_skipped: int = 0
    duration_s: float = 0.0
    status: str = "ok"
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


class Ingester(ABC):
    """Abstract base for every catalog source."""

    #: registry key, also the module name (e.g. "anime_jikan")
    name: ClassVar[str]
    #: one of recsys.schemas.DOMAINS
    domain: ClassVar[str]
    #: short source slug, used in item_uid (e.g. "jikan")
    source: ClassVar[str]

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.n_cached = 0  # cache hits, maintained by subclasses
        self.n_skipped = 0  # raw records that failed mapping

    # ---- subclass contract ------------------------------------------------

    @abstractmethod
    def fetch(self) -> AsyncIterator[dict[str, Any]]:
        """Yield raw source records, one per catalog item.

        Implementations are responsible for pagination, rate limiting and
        caching. Declared as a normal method returning an async iterator so
        subclasses may implement it as an ``async def ... yield`` generator.
        """

    @abstractmethod
    def map_item(self, raw: dict[str, Any]) -> UnifiedItem | None:
        """Map one raw record to a UnifiedItem, or None to skip it."""

    async def aclose(self) -> None:
        """Release network resources. Overridden when a client is held."""

    # ---- generic run loop -------------------------------------------------

    async def run(self, conn: sqlite3.Connection) -> IngestResult:
        """Fetch -> map -> upsert, recording an ingest_runs row.

        Idempotent: re-running against an unchanged upstream updates existing
        rows in place and inserts nothing.
        """
        result = IngestResult(source=self.source, domain=self.domain)
        run_id = start_ingest_run(conn, self.source, self.domain)
        started = time.perf_counter()
        batch: list[UnifiedItem] = []

        try:
            async for raw in self.fetch():
                result.n_fetched += 1
                try:
                    item = self.map_item(raw)
                except Exception as exc:  # one bad record must not kill the run
                    self.n_skipped += 1
                    msg = f"map failed for record {result.n_fetched}: {exc}"
                    log.warning("%s", msg)
                    if len(result.warnings) < 20:
                        result.warnings.append(msg)
                    continue

                if item is None:
                    self.n_skipped += 1
                    continue

                batch.append(item)
                if len(batch) >= BATCH_SIZE:
                    ins, upd = upsert_items(conn, batch)
                    result.n_inserted += ins
                    result.n_updated += upd
                    batch.clear()
                    log.info(
                        "%s: %d fetched, %d inserted, %d updated",
                        self.name, result.n_fetched, result.n_inserted, result.n_updated,
                    )

            if batch:
                ins, upd = upsert_items(conn, batch)
                result.n_inserted += ins
                result.n_updated += upd

        except Exception as exc:
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"
            log.error("%s failed: %s\n%s", self.name, result.error, traceback.format_exc())
        finally:
            await self.aclose()
            result.duration_s = time.perf_counter() - started
            result.n_cached = self.n_cached
            result.n_skipped = self.n_skipped
            finish_ingest_run(
                conn,
                run_id,
                n_fetched=result.n_fetched,
                n_inserted=result.n_inserted,
                n_updated=result.n_updated,
                n_cached=result.n_cached,
                status=result.status,
                error=result.error,
            )

        return result
