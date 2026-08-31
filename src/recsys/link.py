"""Entity resolution: deciding which rows describe the same work.

Loom stores one row per *source*, on purpose -- ``item_uid`` is derived from
``{domain}:{source}:{source_id}``, so a work carried by three sources is three
rows and always will be. That is what makes it possible to compare sources,
re-map one of them, or drop one later. What it must not do is show the same
anime three times in a list.

This module is the reconciliation half. It never merges and never deletes: it
writes a ``links`` row saying "these rows are one work, and this one represents
it", and reads collapse on that. Both halves of a duplicate survive intact for
Phase 3, which will want the *union* of their tags and text rather than only
the winner's.

**Linking is by exact shared id, never by fuzzy title.** ``external_ids.mal_id``
is populated by every anime source: the CSV dump writes the id it was keyed on,
Jikan returns it, and AniList returns it as ``idMal``. So resolution here is an
integer join with no thresholds and no false positives. Fuzzy matching is a
different, much harder problem, and nothing here pretends to solve it.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from recsys.log import get_logger
from recsys.schemas import make_work_uid

log = get_logger(__name__)

#: External-id fields that identify a work across sources, most trustworthy
#: first -- a row carrying several joins on the first it has. Add a key here
#: when a new source brings one; nothing else in this module needs to change.
#:
#: ``isbn`` identifies a printing rather than a work, which is looser than
#: ``mal_id``: two editions of one novel carry different ISBNs and will not be
#: joined. That under-links rather than over-links, which is the safe direction
#: -- a missed link leaves two rows where there should be one, while a false
#: link hides a real book behind an unrelated one.
LINK_KEYS: tuple[str, ...] = ("mal_id", "isbn")


@dataclass
class LinkResult:
    n_works: int = 0  # groups of 2+ rows found
    n_linked: int = 0  # rows belonging to such a group
    n_collapsed: int = 0  # rows hidden from listings (n_linked - n_works)


def _richness(row: sqlite3.Row) -> tuple:
    """Sort key for picking the row that represents a work. Higher is better.

    Ranked by what the row *contains*, never by which source produced it. A
    source-preference list would put source names in the resolution layer and
    make "add a source" mean "edit this file", which is exactly the coupling the
    project invariant forbids. It also happens to be the better rule: the point
    of preferring AniList rows today is that they carry a synopsis, so prefer
    rows that carry a synopsis.
    """
    description = row["description"] or ""
    try:
        n_tags = len(json.loads(row["tags"] or "[]"))
    except json.JSONDecodeError:
        n_tags = 0
    return (
        1 if description.strip() else 0,
        # Tag count before description length, and the order matters. Length
        # looks like a richness signal and is not: the CSV dump's `background`
        # is production trivia ("created by screenplay writer Jun Maeda...") and
        # is routinely *longer* than a real synopsis. Ranking by length handed
        # 120 works to a trivia blurb over AniList's plot summary. Tag count has
        # no such failure mode -- 20 tags against 6 is richer by any reading.
        n_tags,
        len(description.strip()),
        row["n_ratings"] or 0,
        row["popularity"] or 0.0,
        # Deterministic tiebreak, so a rebuild never reshuffles the catalog.
        row["item_uid"],
    )


def _groups(rows: Iterable[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    """Bucket rows by the work they describe.

    A row carrying several linkable ids joins the bucket of the first key in
    ``LINK_KEYS`` it has, so one row never lands in two groups.
    """
    out: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        try:
            ids: dict[str, Any] = json.loads(row["external_ids"] or "{}")
        except json.JSONDecodeError:
            continue
        for key in LINK_KEYS:
            value = ids.get(key)
            if value not in (None, ""):
                out[make_work_uid(row["domain"], key, str(value))].append(row)
                break
    return out


def rebuild_links(conn: sqlite3.Connection) -> LinkResult:
    """Recompute every link from scratch. Idempotent.

    A full rebuild rather than an incremental update: the input is a few tens of
    thousands of rows, it finishes in under a second, and an incremental linker
    would have to reason about a row whose ids changed under it -- complexity
    with nothing to buy it.

    Only groups of two or more get rows. A work with a single source is its own
    representative, and reads treat "no link row" as "show it".
    """
    rows = conn.execute(
        """SELECT item_uid, domain, source, external_ids, description, tags,
                  n_ratings, popularity
           FROM items"""
    ).fetchall()

    result = LinkResult()
    to_insert: list[tuple[str, str, int]] = []

    for work_uid, members in _groups(rows).items():
        if len(members) < 2:
            continue
        winner = max(members, key=_richness)["item_uid"]
        result.n_works += 1
        result.n_linked += len(members)
        for row in members:
            to_insert.append(
                (row["item_uid"], work_uid, 1 if row["item_uid"] == winner else 0)
            )

    result.n_collapsed = result.n_linked - result.n_works

    with conn:  # one transaction: never leave the catalog half-linked
        conn.execute("DELETE FROM links")
        conn.executemany(
            "INSERT INTO links (item_uid, work_uid, is_primary) VALUES (?, ?, ?)",
            to_insert,
        )

    log.info(
        "links: %d works span %d rows; %d rows collapsed out of listings",
        result.n_works, result.n_linked, result.n_collapsed,
    )
    return result


def work_members(conn: sqlite3.Connection, item_uid: str) -> list[str]:
    """Every row describing the same work as ``item_uid``, itself included.

    The read side of what this module records. Phase 3's feature build wants the
    union of a work's rows -- AniList's synopsis and tags alongside the CSV
    dump's 2018 member count -- rather than only the row listings display.
    """
    row = conn.execute("SELECT work_uid FROM links WHERE item_uid = ?", (item_uid,)).fetchone()
    if row is None:
        return [item_uid]
    return [
        r[0]
        for r in conn.execute(
            "SELECT item_uid FROM links WHERE work_uid = ? ORDER BY item_uid", (row["work_uid"],)
        )
    ]
