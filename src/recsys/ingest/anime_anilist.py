"""AniList for the ``anime`` domain -- the source that has text.

Added for one reason the Kaggle dump cannot satisfy: **synopses**. Every anime
table in that archive ships the same 31 columns and none of them is a plot
summary, so ``description`` on the CSV half of the catalog is ``background``
(production trivia) at 7.3% coverage. Content-based similarity across domains
needs real prose, and this is where it comes from.

Two things arrive with it that matter as much:

* **A far richer tag vocabulary.** MAL exposes ~43 genres; AniList adds ranked
  descriptive tags -- *Steins;Gate* comes back with ``Time Manipulation``,
  ``Time Loop``, ``Denpa``, ``Cosmic Horror``. Cross-domain matching works on
  attributes, and these are the attributes.
* **Relation edges.** ``relations`` links a series to the manga or light novel
  it adapts. ``manga_anilist`` mines exactly those to decide what to fetch.

**This is a third source, not a rewrite of the other two.** Rows land under
``anime:anilist:{id}`` beside ``anime:mal:{id}``, because ``item_uid`` is derived
and no source may write into another source's row. Every record carries
``external_ids.mal_id`` from AniList's own ``idMal``, which is what
``recsys.link`` joins on to collapse the duplicates out of listings -- an exact
integer match, not fuzzy title matching.

**Runs after ``anime_mal_csv``.** The ids fetched are the MAL ids already in the
catalog, so this enriches the catalog that exists rather than importing a second
one. With an empty catalog it fetches nothing and says so.
"""

from __future__ import annotations

from recsys.ingest.anilist import SOURCE, AniListIngester
from recsys.log import get_logger

log = get_logger(__name__)

DOMAIN = "anime"


class AniListAnimeIngester(AniListIngester):
    name = "anime_anilist"
    domain = DOMAIN
    media_type = "ANIME"
    # Driven by MyAnimeList ids, because that is what the existing catalog is
    # keyed on and what the resulting rows have to link against.
    id_field = "idMal_in"

    def ids_to_fetch(self) -> list[int]:
        """The MAL ids already in the catalog, most popular first.

        Ordering by popularity means an interrupted or ``--limit``ed run
        enriches the titles anyone would actually look at first.
        """
        from recsys.db import session

        with session() as conn:
            rows = conn.execute(
                """SELECT CAST(json_extract(external_ids, '$.mal_id') AS INTEGER) AS mal_id,
                          MAX(popularity) AS pop
                   FROM items
                   WHERE domain = ?
                     AND source != ?
                     AND json_extract(external_ids, '$.mal_id') IS NOT NULL
                   GROUP BY mal_id
                   ORDER BY pop DESC""",
                (DOMAIN, SOURCE),
            ).fetchall()

        return self._limited([r["mal_id"] for r in rows if r["mal_id"]])
