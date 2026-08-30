"""Jikan again, by id this time: the anime the catalog holds no prose for.

``anime_jikan`` pages ``/top/anime`` and takes whatever the ranking gives it.
This asks for specific titles instead -- the ones where every row of the work
has a null ``description`` -- and it exists because those are exactly the works
the content model cannot see.

**Why there is a gap at all.** The 2018 Kaggle dump ships no synopsis, AniList
is the text source and simply has no entry for some of what that dump knew, and
where AniList has nothing the MAL row wins the primary election with no prose
behind it. 2,935 anime works are left with a document of title and tags only,
and 2,918 of them carry a ``mal_id`` -- which is precisely what Jikan is keyed
on.

**How much of that it can actually recover is Jikan's decision, not ours.**
Measured 2026-08-30: ``/anime/1`` and ``/anime/38000`` answer in under a second,
but 13 of 14 of the ids *this* ingester wants returned HTTP 504. Jikan is
serving its warm cache and timing out on cold lookups, and the works with no
synopsis are obscure by definition -- exactly the cold half. So this is written
to harvest whatever is warm and skip the rest without complaint, and to be
re-run later: the disk cache stores only successes, so a second pass asks again
for precisely the ids that failed. Probe it with real ids from
``ids_to_fetch()`` before assuming it is back; a popular id is always warm and
will tell you it is.

**This is the same shape as ``book_openlibrary``**: a second pass over a domain
already ingested, driven by ids already in the catalog, fetching the one field
the first source could not supply. Rows land at ``anime:jikan:{mal_id}`` beside
the dump's ``anime:mal:{mal_id}``, and ``link.py`` collapses them on the shared
``mal_id`` -- at which point ``_richness`` prefers the row that has a synopsis,
which is the whole point.

Everything about *how* to talk to Jikan -- the limiter, the retry ladder, the
disk cache, the mapper -- is inherited. This subclass is three things: which
ids, one URL shape, and a name.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from recsys.ingest.anime_jikan import DOMAIN, SOURCE, JikanAnimeIngester
from recsys.log import get_logger
from recsys.schemas import utcnow

log = get_logger(__name__)


class JikanAnimeTextIngester(JikanAnimeIngester):
    name = "anime_jikan_text"
    domain = DOMAIN
    source = SOURCE

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.target_count = settings.jikan_text_target_count
        # Fewer retries than the listing ingester, deliberately. A 504 here is
        # Jikan saying "not in my cache", not "I am busy" -- measured on the
        # real target ids, 13 of 14 returned 504 while /anime/1 answered in
        # under a second. Climbing the full ladder on each would turn a 50
        # minute run into a day of backoff for the same nothing.
        self.max_retries = min(self.max_retries, settings.jikan_text_max_retries)
        self.n_unavailable = 0

    def ids_to_fetch(self) -> list[int]:
        """MAL ids of anime works with no prose, best-known first.

        Ordered by calibrated popularity so an interrupted or ``--limit``ed run
        fills in the titles a reader would actually meet, and falling back to
        the raw count where nothing has been calibrated -- the same degradation
        the read path uses, for the same reason.

        Deliberately driven off the row that *represents* the work rather than
        off every row: a work whose AniList row already carries a synopsis is
        not missing prose just because its dump row is bare.
        """
        from recsys.db import session

        with session() as conn:
            rows = conn.execute(
                """SELECT CAST(json_extract(i.external_ids, '$.mal_id') AS INTEGER) AS mal_id,
                          COALESCE(pn.value, i.popularity) AS pop
                     FROM items i
                     LEFT JOIN links l ON l.item_uid = i.item_uid
                     LEFT JOIN popularity_norm pn ON pn.item_uid = i.item_uid
                    WHERE i.domain = ?
                      AND (l.item_uid IS NULL OR l.is_primary = 1)
                      AND i.description IS NULL
                      AND json_extract(i.external_ids, '$.mal_id') IS NOT NULL
                    ORDER BY pop DESC""",
                (DOMAIN,),
            ).fetchall()

        ids = [r["mal_id"] for r in rows if r["mal_id"]]
        return ids[: self.target_count]

    async def fetch(self) -> AsyncIterator[dict[str, Any]]:
        """One detail request per id. No pagination: the ids are the plan."""
        self._fetched_at = utcnow()

        ids = self.ids_to_fetch()
        log.info("jikan text: %d anime works have no synopsis", len(ids))

        for n, mal_id in enumerate(ids, start=1):
            try:
                payload = await self._get_json(f"{self.base_url}/anime/{mal_id}")
            except RuntimeError as exc:
                # One unreachable title must never end a 2,918-title run. The
                # disk cache only ever stores a 200, so re-running retries
                # exactly the ids that failed and nothing else.
                self.n_unavailable += 1
                if self.n_unavailable <= 5:
                    log.warning("jikan text: mal_id %s unavailable (%s)", mal_id, exc)
                continue

            raw = payload.get("data")
            if not isinstance(raw, dict):
                self.n_unavailable += 1
                continue
            yield raw

            if n % 100 == 0:
                log.info("jikan text: %d/%d asked, %d unavailable, %d cache hits",
                         n, len(ids), self.n_unavailable, self.cache.hits)

        log.info("jikan text: done -- %d ids asked, %d unavailable, %d cache hits, %d misses",
                 len(ids), self.n_unavailable, self.cache.hits, self.cache.misses)
