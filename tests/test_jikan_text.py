"""The synopsis-backfill ingester in ingest/anime_jikan_text.py.

Two units, both offline. ``ids_to_fetch`` is a query -- it is driven against a
seeded catalog and asserts *which* works are judged to be missing prose.
``fetch`` is a loop over Jikan detail lookups -- its inherited ``_get_json`` is
stubbed, so the outage behaviour that motivated the whole module (skip a 504,
never end the run) is pinned without a network.
"""

from __future__ import annotations

from typing import Any

import pytest

from recsys.config import Settings
from recsys.db import connect, init_db, upsert_items
from recsys.ingest.anime_jikan_text import JikanAnimeTextIngester
from recsys.schemas import UnifiedItem


def _item(mal_id: int, *, description: str | None, popularity: float,
          domain: str = "anime", with_mal_id: bool = True) -> UnifiedItem:
    return UnifiedItem(
        domain=domain,
        source="mal",
        source_id=str(mal_id),
        title=f"work {mal_id}",
        description=description,
        popularity=popularity,
        external_ids={"mal_id": str(mal_id)} if with_mal_id else {},
    )


@pytest.fixture
def seeded_db(isolated_data_dir):
    """Init the schema at the configured (tmp) path and return a live connection."""
    init_db()
    conn = connect()
    yield conn
    conn.close()


@pytest.fixture
def ingester() -> JikanAnimeTextIngester:
    return JikanAnimeTextIngester(Settings())


# --------------------------------------------------------------------------
# ids_to_fetch
# --------------------------------------------------------------------------


def test_ids_to_fetch_selects_only_prose_less_anime_ordered_by_popularity(
    seeded_db, ingester: JikanAnimeTextIngester
) -> None:
    upsert_items(seeded_db, [
        _item(1, description=None, popularity=100.0),       # in
        _item(2, description=None, popularity=500.0),       # in, most popular
        _item(3, description="already has prose", popularity=999.0),  # out: has text
        _item(6, description=None, popularity=80.0, domain="book"),   # out: wrong domain
        _item(7, description=None, popularity=70.0, with_mal_id=False),  # out: no mal_id
    ])

    assert ingester.ids_to_fetch() == [2, 1]


def test_ids_to_fetch_excludes_non_primary_rows_of_a_linked_work(
    seeded_db, ingester: JikanAnimeTextIngester
) -> None:
    upsert_items(seeded_db, [
        _item(4, description=None, popularity=50.0),   # secondary row of a work
        _item(5, description=None, popularity=60.0),   # primary row of a work
    ])
    with seeded_db:
        seeded_db.execute("INSERT INTO links VALUES (?, ?, ?)", ("anime:mal:4", "w1", 0))
        seeded_db.execute("INSERT INTO links VALUES (?, ?, ?)", ("anime:mal:5", "w1", 1))

    # Only the primary row stands in for the work; the secondary is suppressed.
    assert ingester.ids_to_fetch() == [5]


def test_ids_to_fetch_prefers_calibrated_popularity_over_raw(
    seeded_db, ingester: JikanAnimeTextIngester
) -> None:
    upsert_items(seeded_db, [
        _item(1, description=None, popularity=100.0),
        _item(8, description=None, popularity=10.0),   # low raw, but calibrated high
    ])
    with seeded_db:
        seeded_db.execute("INSERT INTO popularity_norm VALUES (?, ?)", ("anime:mal:8", 9999.0))

    assert ingester.ids_to_fetch() == [8, 1]  # norm lifts 8 above 1


def test_ids_to_fetch_honours_the_target_count(
    seeded_db, ingester: JikanAnimeTextIngester
) -> None:
    upsert_items(seeded_db, [
        _item(i, description=None, popularity=float(i)) for i in range(1, 6)
    ])
    ingester.target_count = 2
    assert ingester.ids_to_fetch() == [5, 4]  # top two by popularity


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------


async def _collect(agen) -> list[Any]:
    return [x async for x in agen]


async def test_fetch_yields_the_reachable_and_skips_the_rest(
    ingester: JikanAnimeTextIngester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ingester, "ids_to_fetch", lambda: [1, 2, 3, 4])

    async def fake_get_json(url: str) -> dict[str, Any]:
        mal_id = int(url.rstrip("/").split("/")[-1])
        if mal_id == 2:
            raise RuntimeError("HTTP 504")     # Jikan cold-cache miss
        if mal_id == 3:
            return {"data": None}              # a 200 with no usable body
        return {"data": {"mal_id": mal_id, "title": f"anime {mal_id}"}}

    monkeypatch.setattr(ingester, "_get_json", fake_get_json)

    out = await _collect(ingester.fetch())

    assert [r["mal_id"] for r in out] == [1, 4]
    assert ingester.n_unavailable == 2  # the 504 and the empty body


async def test_fetch_survives_an_all_unavailable_run(
    ingester: JikanAnimeTextIngester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ingester, "ids_to_fetch", lambda: [10, 11, 12])

    async def always_504(url: str) -> dict[str, Any]:
        raise RuntimeError("HTTP 504")

    monkeypatch.setattr(ingester, "_get_json", always_504)

    out = await _collect(ingester.fetch())

    assert out == []                    # nothing yielded, but no exception raised
    assert ingester.n_unavailable == 3
