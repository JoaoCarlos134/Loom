"""Re-running a mapper over payloads already in the catalog.

Every row keeps the verbatim record it was mapped from in ``raw_json``, with the
stated purpose that a mapping bug can be fixed by re-running the mapper instead
of re-hitting the upstream API. This is that re-run. It exists because the
other route stopped being free: the HTTP cache expires after thirty days, and
past that a one-line mapper fix to 9,049 Open Library rows is 9,049 requests
and most of an hour for data that is sitting in the database.

**Named fields only, never "everything".** A stored payload is not always the
mapper's whole input. ``book_goodbooks`` joins each book to its shelves from a
second file and passes them in beside the row, so remapping one of its rows
yields no tags -- and writing that back would erase ten thousand tag lists to
fix a cover URL. So the caller says which columns the fix touches and nothing
else is written.

Identity is never among them: a remap that changed ``item_uid`` would be an
insert, and everything derived from the old uid would dangle.

Like an ingest, this changes what the derived tables were built from. Rebuild
them afterwards (``scripts/bootstrap.py --no-ingest``).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from recsys.log import get_logger

log = get_logger(__name__)

#: Columns a remap may rewrite. Everything a mapper decides, and nothing that
#: says which row this is or when it was fetched.
REMAPPABLE: tuple[str, ...] = (
    "title", "alt_titles", "year", "creators", "tags", "description",
    "external_ids", "popularity", "mean_rating", "n_ratings", "image_url",
)


#: What "nothing" looks like in a column: NULL, or the JSON for an empty value.
_EMPTY = (None, "", "[]", "{}")


@dataclass
class RemapResult:
    source: str
    domain: str
    n_rows: int = 0  # rows of this source holding a payload
    n_changed: int = 0  # rows where a named field came out different
    n_emptied: int = 0  # ...of which a field that held something now holds nothing
    n_unmapped: int = 0  # payloads today's mapper rejects or cannot place
    dry_run: bool = False


def remap(
    conn: sqlite3.Connection,
    ingester: Any,
    fields: Sequence[str],
    *,
    dry_run: bool = False,
) -> RemapResult:
    """Recompute ``fields`` for every row of ``ingester``'s source. Idempotent.

    ``ingester`` is anything with ``domain``, ``source`` and ``map_item`` -- a
    catalog ingester out of the registry. Its ``fetch`` is never called.
    """
    unknown = [f for f in fields if f not in REMAPPABLE]
    if unknown or not fields:
        raise ValueError(f"cannot remap {unknown or 'nothing'}; choose from {REMAPPABLE}")

    columns = ", ".join(fields)
    rows = conn.execute(
        f"SELECT item_uid, raw_json, {columns} FROM items "
        "WHERE domain = ? AND source = ? AND raw_json IS NOT NULL",
        (ingester.domain, ingester.source),
    ).fetchall()

    result = RemapResult(source=ingester.source, domain=ingester.domain,
                         n_rows=len(rows), dry_run=dry_run)
    updates: list[tuple[Any, ...]] = []
    for row in rows:
        try:
            item = ingester.map_item(json.loads(row["raw_json"]))
        except Exception as exc:  # one bad payload must not end the pass
            log.warning("remap: %s did not map: %s", row["item_uid"], exc)
            item = None
        # A payload that no longer maps, or maps to a different uid, is not
        # this function's to resolve: deleting rows is prune.py's job.
        if item is None or item.item_uid != row["item_uid"]:
            result.n_unmapped += 1
            continue
        mapped = item.to_row()
        if any(mapped[f] != row[f] for f in fields):
            updates.append((*(mapped[f] for f in fields), row["item_uid"]))
            # The signature of remapping a field the payload cannot rebuild:
            # nothing errors, the values simply go. Counted so a dry run shows
            # it -- sometimes it is the fix (a placeholder cover becoming
            # none), and sometimes it is ten thousand tag lists.
            if any(row[f] not in _EMPTY and mapped[f] in _EMPTY for f in fields):
                result.n_emptied += 1

    result.n_changed = len(updates)
    if updates and not dry_run:
        assignments = ", ".join(f"{f} = ?" for f in fields)
        with conn:  # one transaction: a remap is all of a fix or none of it
            conn.executemany(f"UPDATE items SET {assignments} WHERE item_uid = ?", updates)

    log.info("remap %s/%s: %d rows, %d changed, %d unmapped%s",
             result.domain, result.source, result.n_rows, result.n_changed,
             result.n_unmapped, " (dry run)" if dry_run else "")
    return result
