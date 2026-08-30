"""TMDB for the ``tv`` domain.

The same endpoints and the same mapper as film, with four field names TMDB
spells differently -- ``name`` for ``title``, ``first_air_date`` for
``release_date``, keywords under ``results`` rather than ``keywords``, and the
IMDb id behind ``external_ids`` instead of sitting at the top level. Those four
differences are the entire content of this module, which is the point: adding a
domain is a file and a registry line.

Television is worth having separately from film rather than folded into it.
A series and a two-hour film are different commitments, and a recommender that
cannot tell them apart will happily answer "you have three hours free" with a
sixty-episode drama. The ``tv`` domain also overlaps anime heavily in subject
matter, which makes it the most likely place for the content bridge to produce
a connection that is obviously right or obviously wrong -- either being useful.
"""

from __future__ import annotations

from recsys.ingest.tmdb import TmdbIngester
from recsys.log import get_logger

log = get_logger(__name__)

DOMAIN = "tv"


class TmdbTvIngester(TmdbIngester):
    name = "tv_tmdb"
    domain = DOMAIN
    media_path = "tv"
    title_field = "name"
    original_title_field = "original_name"
    date_field = "first_air_date"
    keyword_key = "results"
    # Film exposes `imdb_id` directly; television needs it appended.
    append = "credits,keywords,external_ids"

    def __init__(self, settings) -> None:
        super().__init__(settings)
        # Television's vote counts run an order of magnitude below film's -- a
        # well-known show gathers fewer votes than a mid-tier movie -- so the
        # film threshold would cut the domain in half for the wrong reason.
        self.min_votes = settings.tmdb_tv_min_votes
