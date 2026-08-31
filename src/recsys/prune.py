"""Removing catalog rows that no longer earn their place.

Filters live in the ingesters, where they stop a row being fetched at all. That
is the right place for them and it is useless for rows already on disk: a filter
added today does not reach into yesterday's ingest. This module is the other
half -- it applies the same bars to what is already stored.

**This deletes data, which nothing else in Loom does.** Every other write is an
upsert; ``link.py`` hides duplicates without touching them, and ``popularity.py``
stores a second opinion beside the source datum rather than replacing it. So
pruning is deliberately explicit: it never runs as part of a normal bootstrap,
it reports before it acts, and ``dry_run`` is the default everywhere it can be.

What gets removed, and why each is not merely unpopular but *unusable*:

* **Too few ratings to have a rating.** A film with four votes has a score built
  on nothing, and no audience signal to place it against anything else.
* **Adult titles**, which the catalog does not carry.
* **Formats that are not works** -- AniList files opening-theme clips as anime.

Nothing here judges age or origin. An obscure 1927 film with ten thousand votes
is a better catalog entry than a forgotten 2019 one with three, and a threshold
on votes says so without needing a rule about either.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from recsys.log import get_logger

log = get_logger(__name__)


@dataclass
class PruneRule:
    """One reason a stored row would not be ingested today."""

    name: str
    domain: str | None  # None = every domain
    where: str
    params: tuple = ()


@dataclass
class PruneResult:
    counts: dict[str, int] = field(default_factory=dict)
    deleted: int = 0
    dry_run: bool = True

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def rules(settings) -> list[PruneRule]:
    """The bars a stored row must still clear, mirroring the ingest filters."""
    return [
        PruneRule(
            f"movie: fewer than {settings.tmdb_min_votes} votes",
            "movie",
            "COALESCE(n_ratings, 0) < ?",
            (settings.tmdb_min_votes,),
        ),
        PruneRule(
            f"tv: fewer than {settings.tmdb_tv_min_votes} votes",
            "tv",
            "COALESCE(n_ratings, 0) < ?",
            (settings.tmdb_tv_min_votes,),
        ),
        # Only AniList rows carry `isAdult`; the older MAL dump does not, and a
        # missing flag must not be read as "adult".
        PruneRule(
            "adult titles",
            None,
            "json_extract(raw_json, '$.isAdult') = 1 OR json_extract(raw_json, '$.adult') = 1",
        ),
        PruneRule(
            "music videos filed as anime",
            "anime",
            "json_extract(raw_json, '$.format') = 'MUSIC'",
        ),
    ]


def prune(conn: sqlite3.Connection, settings, *, dry_run: bool = True) -> PruneResult:
    """Count -- and, unless ``dry_run``, delete -- rows that fail the filters.

    Deleting an item leaves every derived table -- ``links``,
    ``popularity_norm``, ``works`` and ``relations`` -- pointing at nothing, so
    all four are rebuilt by the caller afterwards. They are derived and a full
    rebuild is a second or two; carrying cascade rules for a once-in-a-while
    operation would be the more fragile choice.
    """
    result = PruneResult(dry_run=dry_run)
    doomed: set[str] = set()

    for rule in rules(settings):
        sql = f"SELECT item_uid FROM items WHERE ({rule.where})"
        params = list(rule.params)
        if rule.domain:
            sql += " AND domain = ?"
            params.append(rule.domain)
        uids = {r[0] for r in conn.execute(sql, params)}
        # Counted before the union, so a row failing two rules is reported
        # under both but deleted once.
        result.counts[rule.name] = len(uids)
        doomed |= uids

    if not dry_run and doomed:
        with conn:
            for chunk in (list(doomed)[i : i + 500] for i in range(0, len(doomed), 500)):
                marks = ",".join("?" * len(chunk))
                conn.execute(f"DELETE FROM items WHERE item_uid IN ({marks})", chunk)
        result.deleted = len(doomed)
        log.info("prune: removed %d rows", result.deleted)
    else:
        log.info("prune: %d rows would be removed (dry run)", len(doomed))

    return result
