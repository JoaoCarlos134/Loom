"""Ingester registry -- the single seam where new domains plug in.

Phase 1+ adds a source by writing ``recsys/ingest/<name>.py`` and adding one
line here. Nothing else in the codebase names an ingester.
"""

from __future__ import annotations

from typing import Callable

from recsys.config import Settings
from recsys.ingest.base import IngestRunner


def _jikan() -> type[IngestRunner]:
    from recsys.ingest.anime_jikan import JikanAnimeIngester

    return JikanAnimeIngester


def _mal_csv() -> type[IngestRunner]:
    from recsys.ingest.anime_mal_csv import MalCsvAnimeIngester

    return MalCsvAnimeIngester


def _anilist() -> type[IngestRunner]:
    from recsys.ingest.anime_anilist import AniListAnimeIngester

    return AniListAnimeIngester


def _manga_anilist() -> type[IngestRunner]:
    from recsys.ingest.manga_anilist import AniListMangaIngester

    return AniListMangaIngester


def _movie_tmdb() -> type[IngestRunner]:
    from recsys.ingest.movie_tmdb import TmdbMovieIngester

    return TmdbMovieIngester


def _tv_tmdb() -> type[IngestRunner]:
    from recsys.ingest.tv_tmdb import TmdbTvIngester

    return TmdbTvIngester


def _goodbooks() -> type[IngestRunner]:
    from recsys.ingest.book_goodbooks import GoodbooksIngester

    return GoodbooksIngester


def _openlibrary() -> type[IngestRunner]:
    from recsys.ingest.book_openlibrary import OpenLibraryIngester

    return OpenLibraryIngester


def _musicbrainz() -> type[IngestRunner]:
    from recsys.ingest.music_musicbrainz import MusicBrainzIngester

    return MusicBrainzIngester


def _mal_ratings() -> type[IngestRunner]:
    from recsys.ingest.ratings_mal_csv import MalRatingsLoader

    return MalRatingsLoader


# name -> lazy loader. Lazy so an ingester's optional deps are imported only
# when that ingester actually runs.
REGISTRY: dict[str, Callable[[], type[IngestRunner]]] = {
    "anime_jikan": _jikan,
    "anime_mal_csv": _mal_csv,
    "anime_anilist": _anilist,
    "manga_anilist": _manga_anilist,
    "movie_tmdb": _movie_tmdb,
    "tv_tmdb": _tv_tmdb,
    "book_goodbooks": _goodbooks,
    "book_openlibrary": _openlibrary,
    "music_musicbrainz": _musicbrainz,
    "anime_mal_ratings": _mal_ratings,
}


def available() -> list[str]:
    return sorted(REGISTRY)


def get_ingester(name: str, settings: Settings) -> IngestRunner:
    try:
        loader = REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown ingester {name!r}; available: {', '.join(available())}"
        ) from None
    return loader()(settings)
