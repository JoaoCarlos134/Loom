"""AniList for the ``manga`` domain -- Loom's second domain, and its first real
test of the cross-domain premise.

Everything before this was anime. One table, five declared domains, one of them
populated: the promise that ``UnifiedItem`` makes an anime and a novel
comparable had never actually been exercised. Manga is the cheapest honest test,
because adding it costs one file, one registry line and a settings default --
which is the invariant the architecture claims, now checked rather than
asserted.

**What it fetches, and why that is the interesting part.** Not "AniList's manga
catalogue". The ids come from the relation edges already stored in the anime
rows' ``raw_json``: the manga and light novels our anime were *adapted from*.
Those edges were kept during the anime ingest precisely so mining them later
would cost no further requests, and it did not.

That makes this a cross-domain pair with **hard links rather than inferred
similarity**. Everywhere else Loom will have to guess that a book resembles an
anime from shared tags and text; here the two are the same story and AniList
says so outright. It is the only pair likely to have that property, which makes
it the reference case for judging whether the content bridge works at all: if a
model cannot connect an anime to the manga it was literally adapted from, the
tag-and-text approach is not going to connect it to an unrelated novel either.

**Runs after ``anime_anilist``**, since that is what leaves the edges behind.
With no anime rows carrying relations it fetches nothing and says so.

Two consequences worth knowing:

* ``format`` becomes a tag here as it does for anime, so manga rows carry
  ``manga`` / ``novel`` / ``one_shot`` and stay separable from TV and films.
* Manga have no studio, so ``creators`` comes from credited staff instead --
  author and artist. "Same person" is one of the three cross-domain bridges,
  and without staff a manga row would offer none.
"""

from __future__ import annotations

from recsys.ingest.anilist import AniListIngester
from recsys.log import get_logger

log = get_logger(__name__)

DOMAIN = "manga"

#: Relation types worth following from an anime to a manga. ``ADAPTATION`` and
#: ``SOURCE`` are the two directions of "this is the same story"; the rest --
#: ``CHARACTER``, ``SPIN_OFF``, ``OTHER`` -- are looser associations that would
#: dilute the one property making this domain valuable.
ADAPTATION_RELATIONS = ("ADAPTATION", "SOURCE")


class AniListMangaIngester(AniListIngester):
    name = "manga_anilist"
    domain = DOMAIN
    media_type = "MANGA"
    # Driven by AniList's own ids, because the relation edges give those
    # directly. Anime is driven by MAL ids only because the existing catalog
    # was already keyed on them.
    id_field = "id_in"

    def ids_to_fetch(self) -> list[int]:
        """AniList ids of the manga our anime were adapted from.

        Ordered by how many anime point at each one, so an interrupted or
        ``--limit``ed run gets the most-adapted works first.
        """
        from recsys.db import session

        with session() as conn:
            rows = conn.execute(
                # `manga_id`, not `id`: json_each exposes its own `id` column,
                # so GROUP BY id would silently group by the array index of the
                # edge rather than by the manga it points at.
                """SELECT CAST(json_extract(e.value, '$.node.id') AS INTEGER) AS manga_id,
                          COUNT(*) AS n
                   FROM items,
                        json_each(json_extract(items.raw_json, '$.relations.edges')) e
                   WHERE items.source = 'anilist'
                     AND items.domain = 'anime'
                     AND json_extract(e.value, '$.node.type') = 'MANGA'
                     AND json_extract(e.value, '$.relationType') IN (?, ?)
                   GROUP BY manga_id
                   ORDER BY n DESC, manga_id ASC""",
                ADAPTATION_RELATIONS,
            ).fetchall()

        return self._limited([r["manga_id"] for r in rows if r["manga_id"]])
