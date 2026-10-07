"""Ingester registry -- the single seam where new domains plug in.

Phase 1+ adds a source by writing ``recsys/ingest/<name>.py`` and adding one
line here. Nothing else in the codebase names an ingester.

Each entry is a factory ``(Settings) -> IngestRunner``. Returning a constructed
instance rather than the class keeps the imports lazy (an ingester's optional
deps load only when it runs) while staying type-honest: a factory returning a
concrete ``IngestRunner`` satisfies the registry, whereas ``type[IngestRunner]``
does not -- a protocol type is not something you can instantiate.
"""

from __future__ import annotations

from collections.abc import Callable

from recsys.config import Settings
from recsys.ingest.base import IngestRunner


def _jikan(settings: Settings) -> IngestRunner:
    from recsys.ingest.anime_jikan import JikanAnimeIngester

    return JikanAnimeIngester(settings)


def _jikan_text(settings: Settings) -> IngestRunner:
    from recsys.ingest.anime_jikan_text import JikanAnimeTextIngester

    return JikanAnimeTextIngester(settings)


def _mal_csv(settings: Settings) -> IngestRunner:
    from recsys.ingest.anime_mal_csv import MalCsvAnimeIngester

    return MalCsvAnimeIngester(settings)


def _anilist(settings: Settings) -> IngestRunner:
    from recsys.ingest.anime_anilist import AniListAnimeIngester

    return AniListAnimeIngester(settings)


def _manga_anilist(settings: Settings) -> IngestRunner:
    from recsys.ingest.manga_anilist import AniListMangaIngester

    return AniListMangaIngester(settings)


def _movie_tmdb(settings: Settings) -> IngestRunner:
    from recsys.ingest.movie_tmdb import TmdbMovieIngester

    return TmdbMovieIngester(settings)


def _tv_tmdb(settings: Settings) -> IngestRunner:
    from recsys.ingest.tv_tmdb import TmdbTvIngester

    return TmdbTvIngester(settings)


def _goodbooks(settings: Settings) -> IngestRunner:
    from recsys.ingest.book_goodbooks import GoodbooksIngester

    return GoodbooksIngester(settings)


def _openlibrary(settings: Settings) -> IngestRunner:
    from recsys.ingest.book_openlibrary import OpenLibraryIngester

    return OpenLibraryIngester(settings)


def _musicbrainz(settings: Settings) -> IngestRunner:
    from recsys.ingest.music_musicbrainz import MusicBrainzIngester

    return MusicBrainzIngester(settings)


def _mal_ratings(settings: Settings) -> IngestRunner:
    from recsys.ingest.ratings_mal_csv import MalRatingsLoader

    return MalRatingsLoader(settings)


def _goodbooks_ratings(settings: Settings) -> IngestRunner:
    from recsys.ingest.ratings_goodbooks import GoodbooksRatingsLoader

    return GoodbooksRatingsLoader(settings)


# name -> factory. Adding a source is one line here plus its module.
REGISTRY: dict[str, Callable[[Settings], IngestRunner]] = {
    "anime_jikan": _jikan,
    "anime_jikan_text": _jikan_text,
    "anime_mal_csv": _mal_csv,
    "anime_anilist": _anilist,
    "manga_anilist": _manga_anilist,
    "movie_tmdb": _movie_tmdb,
    "tv_tmdb": _tv_tmdb,
    "book_goodbooks": _goodbooks,
    "book_openlibrary": _openlibrary,
    "music_musicbrainz": _musicbrainz,
    "anime_mal_ratings": _mal_ratings,
    "book_goodbooks_ratings": _goodbooks_ratings,
}


def available() -> list[str]:
    return sorted(REGISTRY)


def get_ingester(name: str, settings: Settings) -> IngestRunner:
    try:
        factory = REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown ingester {name!r}; available: {', '.join(available())}"
        ) from None
    return factory(settings)
