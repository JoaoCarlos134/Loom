"""Deezer, for the artists Wikimedia Commons has no picture of.

``music_commons`` gave 1,811 of 6,556 artists a freely licensed portrait and
left the other 72% as generated tiles. 4,501 artists already carry a
``deezer_id`` -- ``music_musicbrainz`` stored it beside the fan count -- and
Deezer serves an artist's picture at an address that is a function of that id.
So this source makes **no request at all**: it reads ids out of the catalog and
writes the address.

**These are not free images, and that was the owner's call to make.** They are
label promotional photographs, shown here by hot-linking Deezer's own image
endpoint, never copied. The first pass left them out for that reason; they were
added on request, as the fallback rather than the default.

**Commons still wins where it has a picture.** ``db.fill_from_siblings`` takes
the first sibling, in uid order, that has the field -- and ``music:commons:``
sorts before ``music:deezer:``. Nothing ranks one source over the other by
name in code; the order is the uid tiebreak the read path already uses, and
``tests/test_covers.py`` pins that a free portrait is never displaced.

Same shape and same rule as ``music_commons``: rows land at
``music:deezer:{mbid}``, join on ``musicbrainz_id``, carry nothing but the
picture, and must never represent the artist.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

from recsys.config import Settings
from recsys.ingest.base import Ingester
from recsys.log import get_logger
from recsys.schemas import Domain, UnifiedItem, utcnow

log = get_logger(__name__)

SOURCE = "deezer"
DOMAIN = "music"


def map_deezer(
    raw: dict[str, Any], *, base_url: str = "https://api.deezer.com", fetched_at: Any = None
) -> UnifiedItem | None:
    """Map one (artist, deezer id) pair to its picture row."""
    mbid = (raw.get("_mbid") or "").strip()
    name = (raw.get("_name") or "").strip()
    deezer_id = str(raw.get("_deezer_id") or "").strip()
    # The id goes into a URL path, so anything that is not a plain number is
    # dropped rather than escaped.
    if not mbid or not name or not deezer_id.isdigit():
        return None

    return UnifiedItem(
        domain=cast(Domain, DOMAIN),
        source=SOURCE,
        source_id=mbid,
        title=name,
        external_ids={
            "musicbrainz_id": mbid,
            "deezer_url": f"https://www.deezer.com/artist/{deezer_id}",
        },
        # `big` is 500 px square: the same size asked of Commons.
        image_url=f"{base_url.rstrip('/')}/artist/{deezer_id}/image?size=big",
        fetched_at=fetched_at or utcnow(),
        raw_json=raw,
    )


class DeezerMusicIngester(Ingester):
    """Turns the Deezer ids already in the catalog into picture rows."""

    name = "music_deezer"
    domain = DOMAIN
    source = SOURCE

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.base_url = settings.deezer_url

    async def fetch(self) -> AsyncIterator[dict[str, Any]]:
        from recsys.db import session

        self._fetched_at = utcnow()
        with session() as conn:
            rows = conn.execute(
                """SELECT json_extract(external_ids, '$.musicbrainz_id') AS mbid,
                          json_extract(external_ids, '$.deezer_id') AS deezer_id,
                          title
                     FROM items
                    WHERE domain = ? AND source != ?
                      AND json_extract(external_ids, '$.musicbrainz_id') IS NOT NULL
                      AND json_extract(external_ids, '$.deezer_id') IS NOT NULL
                    ORDER BY item_uid""",
                (DOMAIN, SOURCE),
            ).fetchall()

        if not rows:
            log.warning("%s: no artist in the catalog carries a deezer_id -- "
                        "music_musicbrainz has to run first", self.name)
            return
        seen: set[str] = set()
        for row in rows:
            if row["mbid"] in seen:
                continue
            seen.add(row["mbid"])
            yield {"_mbid": row["mbid"], "_name": row["title"], "_deezer_id": row["deezer_id"]}

    def map_item(self, raw: dict[str, Any]) -> UnifiedItem | None:
        return map_deezer(
            raw, base_url=self.base_url, fetched_at=getattr(self, "_fetched_at", None)
        )
