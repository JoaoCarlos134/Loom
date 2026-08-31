"""The generic fetch -> map -> upsert loop in ingest/base.py.

Driven by fake ingesters rather than Jikan: the loop's contract is that it works
for *any* source, and the failure modes worth pinning (a bad record mid-stream,
an upstream outage part-way through) are awkward to provoke over a real network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from recsys.config import Settings
from recsys.db import connect, init_db
from recsys.ingest.base import BATCH_SIZE, Ingester
from recsys.schemas import UnifiedItem


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "ingest.db"
    init_db(path)
    c = connect(path)
    yield c
    c.close()


@pytest.fixture
def settings():
    return Settings()


class FakeIngester(Ingester):
    """Yields ``n_records`` synthetic records, optionally failing part-way."""

    name = "fake"
    domain = "anime"
    source = "fake"

    def __init__(self, settings, *, n_records=3, fail_after=None, bad_indices=()):
        super().__init__(settings)
        self.n_records = n_records
        self.fail_after = fail_after
        self.bad_indices = set(bad_indices)
        self.closed = False

    async def fetch(self) -> AsyncIterator[dict[str, Any]]:
        for i in range(self.n_records):
            if self.fail_after is not None and i >= self.fail_after:
                raise RuntimeError("upstream exploded")
            yield {"id": i}

    def map_item(self, raw: dict[str, Any]) -> UnifiedItem | None:
        i = raw["id"]
        if i in self.bad_indices:
            raise ValueError(f"bad record {i}")
        if i < 0:
            return None
        return UnifiedItem(
            domain=self.domain, source=self.source, source_id=str(i), title=f"Item {i}"
        )

    async def aclose(self) -> None:
        self.closed = True


def n_rows(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]


def last_run(conn):
    return conn.execute("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 1").fetchone()


# ---- the happy path ------------------------------------------------------


async def test_successful_run_persists_and_reports(conn, settings):
    result = await FakeIngester(settings, n_records=3).run(conn)
    assert result.status == "ok"
    assert (result.n_fetched, result.n_inserted, result.n_updated) == (3, 3, 0)
    assert n_rows(conn) == 3


async def test_rerun_updates_rather_than_duplicates(conn, settings):
    await FakeIngester(settings, n_records=3).run(conn)
    result = await FakeIngester(settings, n_records=3).run(conn)
    assert (result.n_inserted, result.n_updated) == (0, 3)
    assert n_rows(conn) == 3


async def test_run_records_an_ingest_row(conn, settings):
    await FakeIngester(settings, n_records=2).run(conn)
    row = last_run(conn)
    assert row["status"] == "ok"
    assert row["source"] == "fake" and row["domain"] == "anime"
    assert row["n_fetched"] == 2 and row["n_inserted"] == 2
    assert row["finished_at"] is not None


async def test_client_is_closed_even_on_success(conn, settings):
    ing = FakeIngester(settings, n_records=1)
    await ing.run(conn)
    assert ing.closed


async def test_batch_flush_boundary(conn, settings):
    """More records than BATCH_SIZE must still land exactly once each."""
    n = BATCH_SIZE + 7
    result = await FakeIngester(settings, n_records=n).run(conn)
    assert result.n_inserted == n
    assert n_rows(conn) == n


# ---- one bad record must not kill the run --------------------------------


async def test_mapping_error_skips_only_that_record(conn, settings):
    result = await FakeIngester(settings, n_records=5, bad_indices={2}).run(conn)
    assert result.status == "ok"
    assert result.n_fetched == 5
    assert result.n_inserted == 4
    assert result.n_skipped == 1
    assert n_rows(conn) == 4


async def test_mapping_errors_are_reported_as_warnings(conn, settings):
    result = await FakeIngester(settings, n_records=5, bad_indices={1, 3}).run(conn)
    assert len(result.warnings) == 2
    assert "bad record 1" in result.warnings[0]


async def test_warning_list_is_capped(conn, settings):
    """A systematically broken mapper must not build an unbounded list."""
    result = await FakeIngester(settings, n_records=50, bad_indices=set(range(50))).run(conn)
    assert result.n_skipped == 50
    assert len(result.warnings) == 20


# ---- upstream failure part-way through -----------------------------------


async def test_partial_failure_keeps_the_records_already_fetched(conn, settings):
    """Regression: a mid-run failure used to discard the buffered batch.

    With BATCH_SIZE=200, a run that fetched 25 items and then hit a 504 never
    reached the post-loop flush, so every fetched record was lost. Upstream
    outages are routinely partial -- Jikan serves a cached page 1 and 504s every
    deeper page -- which makes that prefix the only obtainable data.
    """
    result = await FakeIngester(settings, n_records=100, fail_after=25).run(conn)

    assert result.status == "error"
    assert "upstream exploded" in result.error
    assert result.n_fetched == 25
    assert result.n_inserted == 25
    assert n_rows(conn) == 25


async def test_partial_failure_after_a_full_batch(conn, settings):
    """Records flushed before the failure plus the buffered remainder."""
    n = BATCH_SIZE + 10
    result = await FakeIngester(settings, n_records=1000, fail_after=n).run(conn)
    assert result.n_inserted == n
    assert n_rows(conn) == n


async def test_partial_failure_is_recorded_as_an_error_run(conn, settings):
    """Persisting the prefix must not dress a failed run up as a success."""
    await FakeIngester(settings, n_records=100, fail_after=25).run(conn)
    row = last_run(conn)
    assert row["status"] == "error"
    assert "upstream exploded" in row["error"]
    assert row["n_inserted"] == 25


async def test_failed_run_does_not_count_as_a_successful_ingest(conn, settings):
    from recsys.db import last_ingest_at

    await FakeIngester(settings, n_records=100, fail_after=25).run(conn)
    assert last_ingest_at(conn) is None


async def test_immediate_failure_persists_nothing(conn, settings):
    result = await FakeIngester(settings, n_records=100, fail_after=0).run(conn)
    assert result.status == "error"
    assert result.n_fetched == 0
    assert n_rows(conn) == 0


async def test_client_is_closed_after_a_failure(conn, settings):
    ing = FakeIngester(settings, n_records=10, fail_after=2)
    await ing.run(conn)
    assert ing.closed


async def test_a_later_successful_run_completes_the_partial_catalog(conn, settings):
    """The recovery story: re-run once upstream is healthy, get the rest."""
    await FakeIngester(settings, n_records=100, fail_after=25).run(conn)
    result = await FakeIngester(settings, n_records=100).run(conn)

    assert result.status == "ok"
    assert (result.n_inserted, result.n_updated) == (75, 25)
    assert n_rows(conn) == 100


# ---- registry ------------------------------------------------------------


def test_registry_resolves_the_jikan_ingester(settings):
    from recsys.ingest.registry import available, get_ingester

    assert "anime_jikan" in available()
    ing = get_ingester("anime_jikan", settings)
    assert ing.name == "anime_jikan"
    assert (ing.domain, ing.source) == ("anime", "jikan")


def test_unknown_ingester_raises_with_the_available_list(settings):
    from recsys.ingest.registry import get_ingester

    with pytest.raises(KeyError, match="anime_jikan"):
        get_ingester("nope", settings)
