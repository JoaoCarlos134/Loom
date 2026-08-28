"""SQLite catalog: schema, migrations-lite, upsert and query helpers.

Only stdlib ``sqlite3``. No ORM: the catalog is a handful of denormalised
tables, and the future model layer reads it through pyarrow/parquet exports
rather than through row objects.

Migrations-lite: ``PRAGMA user_version`` holds the applied schema version.
``init_db()`` applies every migration newer than that, one transaction each.
Never edit a shipped migration -- append a new one.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from recsys.config import get_settings
from recsys.log import get_logger
from recsys.schemas import UnifiedItem

log = get_logger(__name__)

# Columns returned to the API. raw_json is deliberately excluded: it is a
# debugging / re-mapping aid, not payload.
ITEM_COLUMNS: tuple[str, ...] = (
    "item_uid", "domain", "source", "source_id", "title", "alt_titles", "year",
    "creators", "tags", "description", "external_ids", "popularity",
    "mean_rating", "n_ratings", "image_url", "fetched_at",
)
_SELECT_COLS = ", ".join(f"i.{c}" for c in ITEM_COLUMNS)


# --------------------------------------------------------------------------
# migrations
# --------------------------------------------------------------------------

MIGRATION_1 = """
CREATE TABLE items (
    item_uid     TEXT PRIMARY KEY,
    domain       TEXT NOT NULL,
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    title        TEXT NOT NULL,
    alt_titles   TEXT NOT NULL DEFAULT '[]',
    year         INTEGER,
    creators     TEXT NOT NULL DEFAULT '[]',
    tags         TEXT NOT NULL DEFAULT '[]',
    description  TEXT,
    external_ids TEXT NOT NULL DEFAULT '{}',
    popularity   REAL,
    mean_rating  REAL,
    n_ratings    INTEGER,
    image_url    TEXT,
    fetched_at   TEXT NOT NULL,
    raw_json     TEXT,
    UNIQUE (domain, source, source_id)
);

CREATE INDEX idx_items_domain     ON items(domain);
CREATE INDEX idx_items_title      ON items(title);
CREATE INDEX idx_items_domain_pop ON items(domain, popularity DESC);

CREATE VIRTUAL TABLE items_fts USING fts5(
    title, alt_titles, description,
    content='items', content_rowid='rowid',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, alt_titles, description)
    VALUES (new.rowid, new.title, new.alt_titles, new.description);
END;

CREATE TRIGGER items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, alt_titles, description)
    VALUES ('delete', old.rowid, old.title, old.alt_titles, old.description);
END;

CREATE TRIGGER items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, alt_titles, description)
    VALUES ('delete', old.rowid, old.title, old.alt_titles, old.description);
    INSERT INTO items_fts(rowid, title, alt_titles, description)
    VALUES (new.rowid, new.title, new.alt_titles, new.description);
END;

CREATE TABLE ingest_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    domain      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    n_fetched   INTEGER NOT NULL DEFAULT 0,
    n_inserted  INTEGER NOT NULL DEFAULT 0,
    n_updated   INTEGER NOT NULL DEFAULT 0,
    n_cached    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'running',
    error       TEXT
);

CREATE INDEX idx_runs_domain ON ingest_runs(domain, finished_at DESC);
"""

# Append new migrations here; never modify an existing entry.
MIGRATIONS: list[tuple[int, str]] = [
    (1, MIGRATION_1),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a configured connection. Cheap enough to do once per request."""
    path = Path(db_path) if db_path is not None else get_settings().db_path
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


@contextmanager
def session(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: Path | str | None = None) -> int:
    """Create or migrate the schema. Idempotent; returns the resulting version."""
    with session(db_path) as conn:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for version, script in MIGRATIONS:
            if version <= current:
                continue
            log.info("applying migration %d", version)
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
            current = version
        return int(current)


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------

_UPSERT_SQL = """
INSERT INTO items (
    item_uid, domain, source, source_id, title, alt_titles, year, creators,
    tags, description, external_ids, popularity, mean_rating, n_ratings,
    image_url, fetched_at, raw_json
) VALUES (
    :item_uid, :domain, :source, :source_id, :title, :alt_titles, :year, :creators,
    :tags, :description, :external_ids, :popularity, :mean_rating, :n_ratings,
    :image_url, :fetched_at, :raw_json
)
ON CONFLICT(item_uid) DO UPDATE SET
    title        = excluded.title,
    alt_titles   = excluded.alt_titles,
    year         = excluded.year,
    creators     = excluded.creators,
    tags         = excluded.tags,
    description  = excluded.description,
    external_ids = excluded.external_ids,
    popularity   = excluded.popularity,
    mean_rating  = excluded.mean_rating,
    n_ratings    = excluded.n_ratings,
    image_url    = excluded.image_url,
    fetched_at   = excluded.fetched_at,
    raw_json     = excluded.raw_json
"""


def upsert_items(conn: sqlite3.Connection, items: Iterable[UnifiedItem]) -> tuple[int, int]:
    """Idempotent bulk upsert. Returns (n_inserted, n_updated).

    Existing uids are looked up first so callers can report honest counts --
    sqlite3's ``rowcount`` cannot distinguish an insert from an update in an
    INSERT..ON CONFLICT statement.
    """
    rows = [it.to_row() for it in items]
    if not rows:
        return (0, 0)

    # Deduplicate within the batch: a paginated API can hand back the same item
    # twice when the upstream ranking shifts between page requests.
    by_uid: dict[str, dict[str, Any]] = {r["item_uid"]: r for r in rows}
    rows = list(by_uid.values())

    existing: set[str] = set()
    uids = list(by_uid)
    for chunk in _chunks(uids, 500):
        marks = ",".join(["?"] * len(chunk))
        existing.update(
            r[0]
            for r in conn.execute(
                f"SELECT item_uid FROM items WHERE item_uid IN ({marks})", list(chunk)
            )
        )

    with conn:  # single transaction
        conn.executemany(_UPSERT_SQL, rows)

    n_updated = len(existing)
    return (len(rows) - n_updated, n_updated)


def _chunks(seq: Sequence[Any], n: int) -> Iterator[Sequence[Any]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def start_ingest_run(conn: sqlite3.Connection, source: str, domain: str) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO ingest_runs (source, domain, started_at) VALUES (?, ?, ?)",
            (source, domain, datetime.now(timezone.utc).isoformat()),
        )
    return int(cur.lastrowid)


def finish_ingest_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    n_fetched: int = 0,
    n_inserted: int = 0,
    n_updated: int = 0,
    n_cached: int = 0,
    status: str = "ok",
    error: str | None = None,
) -> None:
    with conn:
        conn.execute(
            """UPDATE ingest_runs
               SET finished_at=?, n_fetched=?, n_inserted=?, n_updated=?,
                   n_cached=?, status=?, error=?
               WHERE id=?""",
            (
                datetime.now(timezone.utc).isoformat(),
                n_fetched, n_inserted, n_updated, n_cached, status, error, run_id,
            ),
        )


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def build_fts_query(q: str) -> str | None:
    """Turn free user text into a safe FTS5 MATCH expression.

    User input never reaches FTS5 as syntax: every token is quoted as a string
    literal, so ``AND`` / ``*`` / ``"`` / ``NEAR`` typed in the search box are
    treated as ordinary words. The final token gets a prefix wildcard so
    as-you-type search matches partial words.
    """
    tokens = _TOKEN_RE.findall(q or "")
    if not tokens:
        return None
    quoted = [f'"{t}"' for t in tokens[:-1]]
    quoted.append(f'"{tokens[-1]}"*')
    return " ".join(quoted)  # FTS5's default operator is AND


def query_items(
    conn: sqlite3.Connection,
    *,
    domain: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, list[UnifiedItem]]:
    """Return (total_matching, page_of_items).

    With ``q``, the FTS index drives the query and results are bm25-ranked with
    title weighted above alt_titles above description. Without ``q``, results
    are ordered by in-source popularity.
    """
    params: list[Any] = []
    where: list[str] = []

    if domain:
        where.append("i.domain = ?")

    match = build_fts_query(q) if q else None
    if q and match is None:
        # e.g. q="!!!" -- nothing searchable in it at all
        return (0, [])

    if match:
        base = "FROM items_fts f JOIN items i ON i.rowid = f.rowid WHERE items_fts MATCH ?"
        params.append(match)
        if domain:
            params.append(domain)
        tail = " AND " + " AND ".join(where) if where else ""
        order = "ORDER BY bm25(items_fts, 10.0, 4.0, 1.0), i.popularity DESC"
    else:
        base = "FROM items i"
        if domain:
            params.append(domain)
        tail = " WHERE " + " AND ".join(where) if where else ""
        order = "ORDER BY i.popularity DESC, i.title ASC"

    total = conn.execute(f"SELECT COUNT(*) {base}{tail}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} {base}{tail} {order} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return (int(total), [UnifiedItem.from_row(r) for r in rows])


def get_item(conn: sqlite3.Connection, item_uid: str) -> UnifiedItem | None:
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM items i WHERE i.item_uid = ?", (item_uid,)
    ).fetchone()
    return UnifiedItem.from_row(row) if row else None


def count_items(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])


def domain_stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT domain,
                  COUNT(*) AS n_items,
                  json_group_array(DISTINCT source) AS sources
           FROM items GROUP BY domain ORDER BY n_items DESC"""
    ).fetchall()
    last = {
        r["domain"]: r["finished_at"]
        for r in conn.execute(
            """SELECT domain, MAX(finished_at) AS finished_at
               FROM ingest_runs WHERE status='ok' GROUP BY domain"""
        )
    }
    return [
        {
            "domain": r["domain"],
            "n_items": r["n_items"],
            "sources": sorted(json.loads(r["sources"])),
            "last_ingest_at": last.get(r["domain"]),
        }
        for r in rows
    ]


def last_ingest_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT MAX(finished_at) FROM ingest_runs WHERE status='ok'"
    ).fetchone()
    return row[0] if row else None
