"""TMDB for the ``movie`` domain.

Films are the first domain Loom holds that shares **no identifier with anything
else in the catalog**. Anime and manga were linked by MAL ids and adaptation
edges, which made the cross-domain pair easy and, in a sense, unrepresentative.
A film and an anime have nothing in common a join could find -- if the two are
to be connected it has to come from what they are *about*.

That makes this the domain that actually tests the bridge. TMDB's ``keywords``
are what it has to work with: *Inception* arrives tagged ``dreams``, ``heist``,
``memory``, ``dream world``, which is the same kind of vocabulary AniList gives
an anime. Genres alone -- 19 buckets -- would not be enough for anything finer
than "both are Action".

``imdb_id`` is carried on every row. MovieLens ships ``links.csv`` mapping its
own ids to IMDb and TMDB, so a ratings source for this domain can join exactly
rather than by title.
"""

from __future__ import annotations

from recsys.ingest.tmdb import TmdbIngester
from recsys.log import get_logger

log = get_logger(__name__)

DOMAIN = "movie"


class TmdbMovieIngester(TmdbIngester):
    name = "movie_tmdb"
    domain = DOMAIN
    media_path = "movie"
    title_field = "title"
    original_title_field = "original_title"
    date_field = "release_date"
    # Film keywords come back under `keywords.keywords`; television uses
    # `keywords.results`. One of TMDB's small asymmetries.
    keyword_key = "keywords"
