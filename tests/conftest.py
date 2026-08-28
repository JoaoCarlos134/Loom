"""Shared fixtures.

Two invariants hold for the whole suite:

* **No test touches ``%LOCALAPPDATA%``.** ``DATA_DIR`` is redirected to a
  ``tmp_path`` before any settings object is built, and the ``get_settings``
  cache is cleared on both sides so a test never inherits or leaks one.
* **No test touches the network.** Everything upstream-shaped is driven from
  ``tests/fixtures/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from recsys.config import get_settings

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    """Read a JSON fixture.

    ``encoding`` is explicit because Python on Windows defaults ``open()`` to
    cp1252, which cannot decode the Japanese titles these fixtures carry.
    """
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def jikan_page() -> dict[str, Any]:
    """The saved /top/anime page: 3 real records."""
    return load_fixture("jikan_top_anime_p1.json")


@pytest.fixture
def jikan_records(jikan_page: dict[str, Any]) -> list[dict[str, Any]]:
    return jikan_page["data"]


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect DATA_DIR to a per-test tmp dir.

    An environment variable rather than an attribute patch, because ``Settings``
    is the only reader of the environment and pydantic-settings gives env vars
    precedence over the repo's ``.env`` -- so this holds even on a machine where
    ``.env`` pins DATA_DIR explicitly.
    """
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    get_settings.cache_clear()

    # setup_logging() attaches a RotatingFileHandler inside DATA_DIR and is
    # process-global via a module flag. Left alone, the first test to build the
    # app would pin an open handle into a tmp dir that pytest then tries to
    # delete -- which fails on Windows. Marking it configured makes every call
    # a no-op for the whole suite.
    monkeypatch.setattr("recsys.log._CONFIGURED", True, raising=False)

    yield data_dir
    get_settings.cache_clear()
