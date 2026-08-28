"""Ingester registry -- the single seam where new domains plug in.

Phase 1+ adds a source by writing ``recsys/ingest/<name>.py`` and adding one
line here. Nothing else in the codebase names an ingester.
"""

from __future__ import annotations

from typing import Callable

from recsys.config import Settings
from recsys.ingest.base import Ingester


def _jikan() -> type[Ingester]:
    from recsys.ingest.anime_jikan import JikanAnimeIngester

    return JikanAnimeIngester


# name -> lazy loader. Lazy so an ingester's optional deps are imported only
# when that ingester actually runs.
REGISTRY: dict[str, Callable[[], type[Ingester]]] = {
    "anime_jikan": _jikan,
    # Phase 1: "books_openlibrary": _openlibrary,
    # Phase 1: "movies_tmdb":       _tmdb,
    # Phase 2: "music_musicbrainz": _musicbrainz,
}


def available() -> list[str]:
    return sorted(REGISTRY)


def get_ingester(name: str, settings: Settings) -> Ingester:
    try:
        loader = REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown ingester {name!r}; available: {', '.join(available())}"
        ) from None
    return loader()(settings)
