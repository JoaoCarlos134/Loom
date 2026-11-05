"""The derived tables, rebuilt in the only order that is right.

``links`` decides which rows are one work; ``popularity_norm`` calibrates on
the works ``links`` found; ``relations`` reads only stored rows; ``works``
stores what all three decided. Every caller that changes the catalog -- a
bootstrap, the full build, the starter pack -- needs exactly this sequence, so
it is written down once, here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from recsys.link import LinkResult, rebuild_links
from recsys.popularity import PopularityResult, rebuild_popularity
from recsys.relations import RelationResult, rebuild_relations
from recsys.works import WorksResult, rebuild_works


@dataclass
class Derived:
    links: LinkResult
    popularity: PopularityResult
    relations: RelationResult
    works: WorksResult


def rebuild_derived(conn: sqlite3.Connection) -> Derived:
    """Rebuild links, popularity, relations and the read view. Idempotent, seconds."""
    links = rebuild_links(conn)
    # Strictly after linking: the calibration anchors are the works that
    # linking just proved two sources both carry.
    popularity = rebuild_popularity(conn)
    # Independent of the two above; it reads only stored rows.
    relations = rebuild_relations(conn)
    # Strictly after all three: the read view stores what they decided -- which
    # row represents a work, what it sorts by, and which story it belongs to.
    works = rebuild_works(conn)
    return Derived(links, popularity, relations, works)
