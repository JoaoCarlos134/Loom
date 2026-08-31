"""The on-disk response cache in ingest/cache.py.

No network and no clock-watching: TTL expiry is exercised by moving the fake
``time.time`` forward, and corruption/atomicity by writing to the shard paths
directly. Every test runs against a tmp dir, never DATA_DIR.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import recsys.ingest.cache as cache_mod
from recsys.ingest.cache import DiskCache

URL = "https://api.example.test/v4/anime/5114"


@pytest.fixture
def cache(tmp_path: Path) -> DiskCache:
    return DiskCache(tmp_path / "cache", "jikan", ttl_days=30)


def test_key_is_deterministic_and_hex(cache: DiskCache) -> None:
    k1 = DiskCache.key(URL)
    assert k1 == DiskCache.key(URL)
    assert k1 != DiskCache.key(URL + "?page=2")
    assert len(k1) == 64 and all(c in "0123456789abcdef" for c in k1)


def test_path_is_sharded_two_levels_deep(cache: DiskCache) -> None:
    k = DiskCache.key(URL)
    path = cache.path_for(URL)
    assert path.parent.name == k[2:4]
    assert path.parent.parent.name == k[:2]
    assert path.name == f"{k}.json"


def test_set_then_get_roundtrips_the_body(cache: DiskCache) -> None:
    body = {"data": {"mal_id": 5114, "title": "Fullmetal Alchemist"}}
    cache.set(URL, body)
    assert cache.get(URL) == body
    assert cache.hits == 1
    assert cache.misses == 0


def test_get_on_absent_url_is_a_miss(cache: DiskCache) -> None:
    assert cache.get(URL) is None
    assert cache.misses == 1
    assert cache.hits == 0


def test_body_survives_non_ascii(cache: DiskCache) -> None:
    body = {"title_japanese": "鋼の錬金術師", "note": "café"}
    cache.set(URL, body)
    assert cache.get(URL) == body


def test_corrupt_entry_is_discarded_and_counts_as_miss(cache: DiskCache) -> None:
    cache.set(URL, {"ok": True})
    path = cache.path_for(URL)
    path.write_text("{ this is not json", encoding="utf-8")

    assert cache.get(URL) is None
    assert cache.misses == 1
    assert not path.exists()  # the bad file is removed, so a re-fetch can replace it


def test_entry_past_ttl_is_a_miss(cache: DiskCache, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000_000.0
    monkeypatch.setattr(cache_mod.time, "time", lambda: now)
    cache.set(URL, {"ok": True})

    # Within the 30-day window: still a hit.
    monkeypatch.setattr(cache_mod.time, "time", lambda: now + 29 * 86400)
    assert cache.get(URL) == {"ok": True}

    # Past it: a miss, so the caller re-fetches.
    monkeypatch.setattr(cache_mod.time, "time", lambda: now + 31 * 86400)
    assert cache.get(URL) is None


def test_ttl_days_zero_never_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = DiskCache(tmp_path / "c", "jikan", ttl_days=0)
    monkeypatch.setattr(cache_mod.time, "time", lambda: 1000.0)
    cache.set(URL, {"ok": True})
    monkeypatch.setattr(cache_mod.time, "time", lambda: 1000.0 + 3650 * 86400)
    assert cache.get(URL) == {"ok": True}


def test_set_is_atomic_leaving_no_temp_files(cache: DiskCache) -> None:
    cache.set(URL, {"ok": True})
    assert list(cache.dir.rglob("*.tmp")) == []


def test_set_overwrites_in_place(cache: DiskCache) -> None:
    cache.set(URL, {"v": 1})
    cache.set(URL, {"v": 2})
    assert cache.get(URL) == {"v": 2}
    assert len(list(cache.dir.rglob("*.json"))) == 1


def test_clear_removes_all_entries_and_returns_count(cache: DiskCache) -> None:
    cache.set(URL, {"a": 1})
    cache.set(URL + "?page=2", {"b": 2})
    assert cache.clear() == 2
    assert list(cache.dir.rglob("*.json")) == []
