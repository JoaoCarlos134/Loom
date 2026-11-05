"""MusicBrainz for the ``music`` domain -- scoped to artists the catalog already
points at.

**Why this is not a music catalog.** The obvious build was the Last.fm 360K
dump: 17.5 million rows, aggregate by artist, take the top ten thousand. It was
measured and abandoned, because it would have been filler:

* **Music tags do not share a vocabulary with the other domains.** Of the tags
  MusicBrainz returns for a sample of artists, 22% appear somewhere in the
  catalog's vocabulary -- but the overlap is ``american``, ``german``,
  ``england`` (nationality), ``2000s``/``2010s`` (decade), ``composer``,
  ``singer`` (occupation) and outright noise (``bootleg``, ``list``). Music tags
  describe *sound*; anime, book and film tags describe *story*. Matching an
  anime to Radiohead because both are ``experimental`` is a coincidence, not a
  recommendation.
* **There is no text.** MusicBrainz carries no biography, so the strongest
  bridge -- embedding a synopsis and a blurb into one space -- has nothing to
  embed here.
* **There are no ratings we can honestly store.** Play counts are implicit
  feedback with no upper bound and no statement of liking; rescaling them into
  ``ratings.rating``, a 0-10 opinion, would invent an opinion nobody expressed.

What survives that critique is exactly the soundtrack link: **2,859 composers
are already named in the crew of 78% of the films and shows in the catalog.**
Those artists have a hard, non-inferred connection to works Loom holds -- the
same property that made manga worth adding and a popularity list not. So the
domain is driven by them, the way ``manga_anilist`` is driven by adaptation
edges.

Two services, both keyless: MusicBrainz for identity and tags (1 request/second,
which it asks for and this respects), Deezer for an audience count, since
MusicBrainz has no popularity signal at all.

Matching is by name, which is the one weak joint here. It is guarded: a result
counts only when MusicBrainz's own match score clears a threshold *and* the
returned name equals the credited name, because "John Williams" is several
different people.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx

from recsys.config import Settings
from recsys.ingest.base import Ingester
from recsys.ingest.cache import DiskCache
from recsys.ingest.ratelimit import RateLimiter, backoff_delay, parse_retry_after
from recsys.log import get_logger
from recsys.net import enable_os_truststore
from recsys.schemas import Creator, Domain, UnifiedItem, utcnow

log = get_logger(__name__)

SOURCE = "musicbrainz"
DOMAIN = "music"

#: Seconds to wait at minimum when a service asks us to slow down.
SLOWDOWN_FLOOR = 2.0

#: Crew jobs and staff roles that mean "wrote the music".
COMPOSER_PATTERNS = ("%Composer%", "%Music%", "%Theme Song%")

#: MusicBrainz tags that describe paperwork rather than sound. Nationality and
#: decade tags are the bulk of the spurious overlap with the other domains'
#: vocabularies, so they are dropped rather than allowed to create matches.
_TAG_NOISE = frozenset({
    "bootleg", "list", "seen live", "live", "misc", "unknown", "other",
    "male", "female", "male vocalists", "female vocalists", "singer",
    "composer", "lyricist", "producer", "artist", "band", "group", "person",
})


def _is_useful_tag(tag: str) -> bool:
    name = tag.strip().lower()
    if not name or name in _TAG_NOISE:
        return False
    # "2000s", "80s", "1990s" -- a decade is not a genre.
    return not name.rstrip("s").isdigit()


def normalise_name(name: str) -> str:
    return " ".join(name.split()).casefold()


#: Deezer's stand-in when it has no picture: an empty image hash, or the md5 of
#: the empty string. Either way a silhouette, not the artist.
_DEEZER_PLACEHOLDERS = ("/images/artist//", "/d41d8cd98f00b204e9800998ecf8427e/")


def deezer_picture(value: Any) -> str | None:
    """A Deezer artist picture URL, or ``None`` for none or the placeholder."""
    if not isinstance(value, str) or not value.startswith("https://"):
        return None
    if any(mark in value for mark in _DEEZER_PLACEHOLDERS):
        return None
    return value


def map_artist(
    raw: dict[str, Any], *, fetched_at: Any = None, min_tag_count: int = 1
) -> UnifiedItem | None:
    """Map one matched MusicBrainz artist to a UnifiedItem."""
    mbid = raw.get("id")
    name = (raw.get("name") or "").strip()
    if not mbid or not name:
        return None

    tags = [
        t["name"].strip()
        for t in (raw.get("tags") or [])
        if isinstance(t, dict)
        and isinstance(t.get("name"), str)
        and _is_useful_tag(t["name"])
        and (t.get("count") or 0) >= min_tag_count
    ]
    # The artist's own kind is a genuine facet: a Person who scores films and an
    # Orchestra are different things.
    if isinstance(raw.get("type"), str) and raw["type"].strip():
        tags.append(raw["type"].strip())

    external_ids = {
        "musicbrainz_id": str(mbid),
        "musicbrainz_url": f"https://musicbrainz.org/artist/{mbid}",
    }
    if isinstance(raw.get("_deezer_id"), int):
        external_ids["deezer_id"] = str(raw["_deezer_id"])

    fans = raw.get("_deezer_fans")
    scored = raw.get("_scored_titles")

    return UnifiedItem(
        domain=cast(Domain, DOMAIN),
        source=SOURCE,
        source_id=str(mbid),
        title=name,
        alt_titles=[a.strip() for a in (raw.get("aliases_seen") or []) if a.strip()],
        year=None,  # an artist is not published in a year
        # The artist is both the item and its creator, so the "same person"
        # bridge reaches music without a special case -- which is the entire
        # reason this domain is worth having.
        creators=[Creator(name=name, role="artist")],
        tags=tags,
        description=None,  # MusicBrainz has no biography
        external_ids=external_ids,
        # Deezer fan count: a real audience signal. MusicBrainz has none, and
        # ranking composers by how many films they scored would measure
        # prolificacy rather than reach.
        popularity=float(fans) if isinstance(fans, int) and fans > 0 else None,
        mean_rating=None,  # nobody rated anything here
        n_ratings=None,
        # Hot-linked, never downloaded: Deezer does not allow its images to be
        # stored. The URL is the whole of what Loom keeps.
        image_url=deezer_picture(raw.get("_deezer_picture")),
        fetched_at=fetched_at or utcnow(),
        # Keeps the match score and which titles they scored, so a bad match can
        # be found later without re-querying.
        raw_json={**raw, "_scored_titles": scored},
    )


class MusicBrainzIngester(Ingester):
    """Resolves composers credited in the catalog to MusicBrainz artists."""

    name = "music_musicbrainz"
    domain = DOMAIN
    source = SOURCE

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.url = settings.musicbrainz_url.rstrip("/")
        self.user_agent = f"Loom/0.1 ( {settings.musicbrainz_contact} )"
        self.deezer_url = settings.deezer_url.rstrip("/")
        self.use_deezer = settings.music_use_deezer
        self.target_count = settings.music_target_count
        self.min_score = settings.musicbrainz_min_score
        self.max_retries = settings.musicbrainz_max_retries
        self.limiter = RateLimiter(
            settings.musicbrainz_rate_per_sec, settings.musicbrainz_rate_per_min
        )
        # Deezer's own ceiling. MusicBrainz's 1/s used to pace these calls by
        # accident; once its answers come from the cache, nothing else would.
        self.deezer_limiter = RateLimiter(settings.deezer_rate_per_sec, 0.0)
        self.cache = DiskCache(settings.cache_dir, SOURCE, settings.http_cache_ttl_days)
        self._client: httpx.AsyncClient | None = None

    # ---- what to fetch ----------------------------------------------------

    def composers_in_catalog(self) -> list[tuple[str, int]]:
        """(name, titles scored) for everyone credited with the music, busiest first.

        Read in one pass on its own connection before the run loop starts
        writing, for the same reason the AniList ingesters do it.
        """
        from recsys.db import session

        like = " OR ".join(["json_extract(cr.value, '$.job') LIKE ?"] * len(COMPOSER_PATTERNS))
        with session() as conn:
            rows = conn.execute(
                f"""SELECT json_extract(cr.value, '$.name') AS name, COUNT(*) AS n
                    FROM items,
                         json_each(json_extract(items.raw_json, '$.credits.crew')) cr
                    WHERE items.domain IN ('movie', 'tv') AND ({like})
                    GROUP BY name
                    HAVING name IS NOT NULL AND name != ''
                    ORDER BY n DESC, name ASC""",
                COMPOSER_PATTERNS,
            ).fetchall()

        pairs = [(r["name"].strip(), r["n"]) for r in rows if (r["name"] or "").strip()]
        return pairs[: self.target_count] if self.target_count else pairs

    # ---- http -------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            enable_os_truststore()
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                # MusicBrainz requires a User-Agent naming the application AND
                # a way to contact whoever runs it. Without the contact part it
                # throttles hard: measured 3 of 4 requests returning 503 with a
                # bare agent string, and 4 of 4 succeeding once a URL was added.
                headers={
                    "Accept": "application/json",
                    "User-Agent": self.user_agent,
                },
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_json(self, url: str, params: dict[str, Any], *, limited: bool) -> Any:
        cached = self.cache.get(f"{url}?{sorted(params.items())}")
        if cached is not None:
            self.n_cached += 1
            return cached

        client = self._get_client()
        last_error = "unknown"
        for attempt in range(self.max_retries):
            await (self.limiter if limited else self.deezer_limiter).acquire()
            try:
                resp = await client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(backoff_delay(attempt))
                continue

            if resp.status_code == 200:
                payload = resp.json()
                self.cache.set(f"{url}?{sorted(params.items())}", payload)
                return payload

            # MusicBrainz returns 503 routinely under load; it is a request to
            # slow down, not an error. `backoff_delay` jitters from zero, which
            # is right for avoiding a thundering herd and wrong here -- it can
            # retry almost immediately and burn every attempt inside one bad
            # second. A floor makes the client actually wait.
            if resp.status_code in (429, 503) or resp.status_code >= 500:
                delay = parse_retry_after(resp.headers.get("Retry-After"))
                if delay is None:
                    delay = SLOWDOWN_FLOOR + backoff_delay(attempt, base=4.0)
                await asyncio.sleep(delay)
                last_error = f"HTTP {resp.status_code}"
                continue

            raise RuntimeError(f"{url} returned HTTP {resp.status_code}")

        raise RuntimeError(f"request failed after {self.max_retries} attempts ({last_error})")

    async def _search_artist(self, name: str) -> dict[str, Any] | None:
        """Best MusicBrainz match for a credited name, or None if unconvincing.

        Guarded twice over. MusicBrainz scores every candidate, and a name that
        does not match exactly after normalisation is rejected regardless of
        score -- "John Williams" resolves to several unrelated people, and a
        wrong match here would put an unrelated artist's genres onto a film's
        composer and teach the bridge something false.
        """
        payload = await self._get_json(
            f"{self.url}/artist/",
            {"query": f'artist:"{name}"', "fmt": "json", "limit": 3},
            limited=True,
        )
        for candidate in (payload or {}).get("artists") or []:
            if not isinstance(candidate, dict):
                continue
            if (candidate.get("score") or 0) < self.min_score:
                continue
            if normalise_name(candidate.get("name") or "") == normalise_name(name):
                return candidate
        return None

    async def _deezer_fans(
        self, name: str
    ) -> tuple[int | None, int | None, str | None]:
        """(fan count, deezer id, picture URL) for an artist, or all ``None``.

        Paced by its own limiter at Deezer's documented 50 requests per five
        seconds. A failure here costs a popularity number and a picture, not
        the run.
        """
        try:
            payload = await self._get_json(
                f"{self.deezer_url}/search/artist", {"q": name, "limit": 1}, limited=False
            )
        except Exception as exc:
            log.debug("deezer lookup failed for %r: %s", name, exc)
            return (None, None, None)
        for entry in (payload or {}).get("data") or []:
            if not isinstance(entry, dict):
                continue
            if normalise_name(entry.get("name") or "") == normalise_name(name):
                fans = entry.get("nb_fan")
                # 500x500: sharp on a card, a quarter of the bytes of the 1000px
                # picture. Falls back to whatever size the answer carries.
                picture = next(
                    (entry[k] for k in ("picture_big", "picture_xl", "picture_medium")
                     if deezer_picture(entry.get(k))),
                    None,
                )
                return (int(fans) if isinstance(fans, int) else None, entry.get("id"), picture)
        return (None, None, None)

    # ---- Ingester contract ------------------------------------------------

    async def fetch(self) -> AsyncIterator[dict[str, Any]]:
        self._fetched_at = utcnow()
        composers = self.composers_in_catalog()
        if not composers:
            log.warning(
                "%s: no composers in the catalog -- movie_tmdb / tv_tmdb have to "
                "run first; this domain is driven by their crew credits", self.name
            )
            return

        log.info("%s: resolving %d credited composers", self.name, len(composers))
        matched = 0
        failed = 0
        for n, (name, scored) in enumerate(composers, start=1):
            # One artist that cannot be resolved -- a name MusicBrainz does not
            # know, or a bad minute on their side -- must not end a run of
            # several thousand. A flaky upstream should cost coverage, not the
            # whole ingest.
            try:
                artist = await self._search_artist(name)
            except Exception as exc:
                failed += 1
                self.n_skipped += 1
                if failed <= 5:
                    log.warning("%s: giving up on %r: %s", self.name, name, exc)
                elif failed == 6:
                    log.warning("%s: further lookup failures will not be logged", self.name)
                continue
            if artist is None:
                self.n_skipped += 1
            else:
                fans, deezer_id, picture = (None, None, None)
                if self.use_deezer:
                    fans, deezer_id, picture = await self._deezer_fans(
                        artist.get("name") or name
                    )
                artist["_scored_titles"] = scored
                artist["_credited_as"] = name
                artist["_deezer_fans"] = fans
                artist["_deezer_id"] = deezer_id
                artist["_deezer_picture"] = picture
                matched += 1
                yield artist
            if n % 200 == 0:
                log.info("%s: %d/%d resolved, %d matched, %d unmatched",
                         self.name, n, len(composers), matched, self.n_skipped)

        log.info("%s: %d of %d names resolved to an artist (%d lookup failures)",
                 self.name, matched, len(composers), failed)

    def map_item(self, raw: dict[str, Any]) -> UnifiedItem | None:
        return map_artist(raw, fetched_at=getattr(self, "_fetched_at", None))
