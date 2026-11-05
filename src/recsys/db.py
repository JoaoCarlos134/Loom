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
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recsys.config import get_settings
from recsys.log import get_logger
from recsys.schemas import LOCAL_USER_UID, UnifiedItem

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

MIGRATION_2 = """
CREATE TABLE ratings (
    user_uid TEXT NOT NULL,
    item_uid TEXT NOT NULL,
    rating   REAL NOT NULL,
    rated_at TEXT,
    PRIMARY KEY (user_uid, item_uid)
) WITHOUT ROWID;
"""

MIGRATION_3 = """
CREATE TABLE links (
    item_uid   TEXT PRIMARY KEY,
    work_uid   TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_links_work ON links(work_uid);
"""

MIGRATION_4 = """
CREATE TABLE popularity_norm (
    item_uid TEXT PRIMARY KEY,
    value    REAL NOT NULL
) WITHOUT ROWID;
"""

# `works` is a read cache, not a new source of truth: one row per work, with the
# ordering key stored *on the row*. Reads used to order by
# `COALESCE(popularity_norm.value, items.popularity)`, and because that key
# lives in a joined table no index on `items` could ever satisfy the sort -- so
# every page of the catalog scanned all rows and sorted them in a temp b-tree.
# Measured on the 84k-work catalog: the first page cost 267 ms, 213 ms of it
# the sort alone, and 5.5 ms through this table -- of which the page query is
# 0.48 ms and the rest is the COUNT and building 50 pydantic models. Rebuilt by
# `recsys.works`, strictly after `links` and `popularity_norm`, which it is
# derived from.
#
# `relations` promotes the relation edges already sitting in AniList rows'
# `raw_json` into something joinable. They are *not* `links`: `links` means
# "these rows are the same work", and an adaptation or a sequel is a different
# work. Reading them meant a `json_each` walk of a 631 MB column (1.2 s for all
# 57,480 edges); they carry both the adaptation bridge between domains and the
# franchise edges a recommender needs in order to stop suggesting a work's own
# sequels back to it.
#
# `idx_items_domain` is dropped as redundant: it is a strict left prefix of
# `idx_items_domain_pop`, so every query it served that index serves too.
MIGRATION_5 = """
CREATE TABLE works (
    item_uid TEXT PRIMARY KEY,
    domain   TEXT NOT NULL,
    title    TEXT NOT NULL,
    sort_key REAL
) WITHOUT ROWID;

CREATE INDEX idx_works_sort        ON works(sort_key DESC, title ASC);
CREATE INDEX idx_works_domain_sort ON works(domain, sort_key DESC, title ASC);

CREATE TABLE relations (
    src_item_uid TEXT NOT NULL,
    dst_item_uid TEXT NOT NULL,
    relation     TEXT NOT NULL,
    PRIMARY KEY (src_item_uid, dst_item_uid, relation)
) WITHOUT ROWID;

CREATE INDEX idx_relations_dst ON relations(dst_item_uid);

DROP INDEX idx_items_domain;
"""

# `relations`' primary key leads with `src_item_uid`, so a query selecting *by
# relation type* -- which is what the adaptation bridge asks for -- could not
# use it. Without this the planner drove the join from all 30,875 anime rows
# rather than from the 16,524 adaptation edges; with it, `adaptation_pairs`
# went 200 ms -> 89 ms (against 229 ms for the json_each walk it replaced).
# A separate migration rather than an edit to 5, because 5 has been applied.
MIGRATION_6 = """
CREATE INDEX idx_relations_relation ON relations(relation, src_item_uid);
"""

# `id_map` holds identity claims made by a third party rather than by either
# row: "the row in `domain` whose external_ids[alias_key] is alias_value is the
# work that `work_domain` knows as key=value". Wikidata is the first such
# source -- TMDB rows carry no MAL id, so the anime and its own TMDB entry
# could not be linked on anything the rows themselves say. Exact ids, never
# titles. `source` scopes a reload, so each map replaces only its own rows.
MIGRATION_7 = """
CREATE TABLE id_map (
    source      TEXT NOT NULL,
    work_domain TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    domain      TEXT NOT NULL,
    alias_key   TEXT NOT NULL,
    alias_value TEXT NOT NULL,
    PRIMARY KEY (source, work_domain, key, value, domain, alias_key, alias_value)
) WITHOUT ROWID;
"""

# `works` gains what results and listings need to treat a story as one thing.
# `group_uid` is the connected component of a work's adaptation, franchise and
# series edges, in representative space -- what results suppress and collapse
# to one entry. `series_*` and `listed` fold a book series behind one card:
# goodbooks-10k files every volume as a book, so Death Note was eight cards.
# Recreated rather than altered because it is a cache: it comes back empty and
# reads fall back to the live joins until `bootstrap.py` rebuilds it, which
# every bootstrap does in seconds.
MIGRATION_8 = """
DROP TABLE works;

CREATE TABLE works (
    item_uid    TEXT PRIMARY KEY,
    domain      TEXT NOT NULL,
    title       TEXT NOT NULL,
    sort_key    REAL,
    group_uid   TEXT,
    series_uid  TEXT,
    series_name TEXT,
    series_pos  REAL,
    listed      INTEGER NOT NULL DEFAULT 1,
    n_series    INTEGER
) WITHOUT ROWID;

CREATE INDEX idx_works_sort        ON works(listed, sort_key DESC, title ASC);
CREATE INDEX idx_works_domain_sort ON works(listed, domain, sort_key DESC, title ASC);
CREATE INDEX idx_works_group       ON works(group_uid);
CREATE INDEX idx_works_series      ON works(series_uid);
"""

# The view Loom opens on. All domains were ordered by one calibrated
# popularity, but calibration only puts the sources of *one* domain on one
# scale; across domains the largest raw numbers won, and the Browse tab opened
# on a wall of imageless music. `browse_rank` orders the all-domains listing:
# works with an image first, domains interleaved by each one's own popularity
# rank. Emptied, like 8, so reads fall back until the next rebuild fills it.
MIGRATION_9 = """
DELETE FROM works;
ALTER TABLE works ADD COLUMN has_image INTEGER NOT NULL DEFAULT 0;
ALTER TABLE works ADD COLUMN browse_rank INTEGER;
CREATE INDEX idx_works_browse ON works(listed, browse_rank);
"""

# What search needs to know about a title, computed once at rebuild rather than
# guessed per query. `title_tokens` counts words the way FTS5 does ("Steins;Gate"
# is two, which counting spaces made one); `title_key` and `alt_keys` are the
# title and alternative titles normalised the same way, so "the title *is* what
# was typed" is an equality test. `is_collection` marks boxed sets, which a
# series' search hit should not be. `idx_works_sort` is dropped: since the
# all-domains listing orders by `browse_rank`, no query reads it.
MIGRATION_10 = """
DELETE FROM works;
ALTER TABLE works ADD COLUMN title_tokens INTEGER;
ALTER TABLE works ADD COLUMN title_key TEXT;
ALTER TABLE works ADD COLUMN alt_keys TEXT;
ALTER TABLE works ADD COLUMN is_collection INTEGER NOT NULL DEFAULT 0;
DROP INDEX idx_works_sort;
"""

# Append new migrations here; never modify an existing entry.
MIGRATIONS: list[tuple[int, str]] = [
    (1, MIGRATION_1),
    (2, MIGRATION_2),
    (3, MIGRATION_3),
    (4, MIGRATION_4),
    (5, MIGRATION_5),
    (6, MIGRATION_6),
    (7, MIGRATION_7),
    (8, MIGRATION_8),
    (9, MIGRATION_9),
    (10, MIGRATION_10),
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


_UPSERT_RATING_SQL = """
INSERT INTO ratings (user_uid, item_uid, rating, rated_at)
VALUES (?, ?, ?, ?)
ON CONFLICT(user_uid, item_uid) DO UPDATE SET
    rating   = excluded.rating,
    rated_at = excluded.rated_at
"""


def upsert_ratings(
    conn: sqlite3.Connection, rows: Sequence[tuple[str, str, float, str | None]]
) -> int:
    """Idempotent bulk upsert of (user_uid, item_uid, rating, rated_at) tuples.

    Returns the number of rows written. Unlike :func:`upsert_items` this does
    *not* split inserts from updates: telling them apart means probing for
    existing keys first, and at ~19M ratings that probe costs more than the
    write it annotates. Rating sources are static dumps, so the split would
    only ever read "all inserted" or "all updated" anyway.
    """
    if not rows:
        return 0
    with conn:  # single transaction
        conn.executemany(_UPSERT_RATING_SQL, rows)
    return len(rows)


#: One ``id_map`` claim: (work_domain, key, value, domain, alias_key, alias_value).
IdMapRow = tuple[str, str, str, str, str, str]


def replace_id_map(conn: sqlite3.Connection, source: str, rows: Iterable[IdMapRow]) -> int:
    """Replace every ``id_map`` claim ``source`` makes. Returns rows written.

    Wholesale, like the derived tables: a map is a snapshot of what its source
    asserts today, and a claim it has since withdrawn must not survive a reload.
    Other sources' claims are untouched.
    """
    unique = sorted(set(rows))
    with conn:
        conn.execute("DELETE FROM id_map WHERE source = ?", (source,))
        conn.executemany(
            "INSERT INTO id_map (source, work_domain, key, value, domain, alias_key, "
            "alias_value) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(source, *row) for row in unique],
        )
    return len(unique)


def set_rating(
    conn: sqlite3.Connection,
    item_uid: str,
    rating: float,
    *,
    user_uid: str = LOCAL_USER_UID,
    rated_at: str | None = None,
) -> str:
    """Record what you think of a work. Returns the uid actually written.

    The uid is resolved to the row that *represents* the work before it is
    stored. A work has several rows and ``get_item`` reaches every one of them
    by uid, so without this, rating ``anime:anilist:5114`` and then
    ``anime:mal:5114`` would store two ratings of one work -- the primary key
    cannot collapse them, because it is looking at two different uids. Resolving
    first makes rating the same work twice an update, which is what a person
    doing it means.
    """
    from recsys.link import representative_uid

    uid = representative_uid(conn, item_uid)
    upsert_ratings(conn, [(user_uid, uid, float(rating), rated_at or datetime.now(UTC).isoformat())])
    return uid


def delete_rating(
    conn: sqlite3.Connection, item_uid: str, *, user_uid: str = LOCAL_USER_UID
) -> bool:
    """Forget a rating. True if there was one to forget.

    The one deletion outside ``prune.py``, and it is not the same act: pruning
    removes catalog rows that derived tables point at, which is why it is
    hedged behind a dry run. A rating is your own datum and nothing is derived
    from it on disk, so un-rating something has to actually remove it -- there
    is no "hidden but present" state for a rating that would mean anything.

    Resolves the uid exactly as :func:`set_rating` wrote it, so un-rating works
    when you pass a different row of the same work than the one you rated.
    """
    from recsys.link import representative_uid

    with conn:
        cur = conn.execute(
            "DELETE FROM ratings WHERE user_uid = ? AND item_uid = ?",
            (user_uid, representative_uid(conn, item_uid)),
        )
    return cur.rowcount > 0


def get_rating(
    conn: sqlite3.Connection, item_uid: str, *, user_uid: str = LOCAL_USER_UID
) -> tuple[float, str | None] | None:
    """Your score for a work and when you set it, or ``None`` if you have not.

    Resolves the uid the way :func:`set_rating` stored it, so opening any row
    of a work shows the rating you gave through any other.
    """
    from recsys.link import representative_uid

    row = conn.execute(
        "SELECT rating, rated_at FROM ratings WHERE user_uid = ? AND item_uid = ?",
        (user_uid, representative_uid(conn, item_uid)),
    ).fetchone()
    return None if row is None else (float(row["rating"]), row["rated_at"])


def query_ratings(
    conn: sqlite3.Connection,
    *,
    user_uid: str = LOCAL_USER_UID,
    domain: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, list[tuple[float, str | None, UnifiedItem]]]:
    """Your library -- ratings joined to the catalog rows they name.

    Newest first, because a library is read as a record of what you have been
    doing lately rather than as a ranking.

    An inner join, so a rating pointing at a row that is not in the catalog --
    pruned since, or never ingested -- does not appear. That is a real case:
    the ratings table also holds 19.2M MAL ratings covering anime this catalog
    may not carry, and ``user_uid`` is the only thing separating them.
    """
    where = ["r.user_uid = ?"]
    params: list[Any] = [user_uid]
    if domain:
        where.append("i.domain = ?")
        params.append(domain)
    tail = " WHERE " + " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(*) FROM ratings r JOIN items i ON i.item_uid = r.item_uid{tail}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"""SELECT r.rating AS my_rating, r.rated_at AS my_rated_at, {_SELECT_COLS}
            FROM ratings r JOIN items i ON i.item_uid = r.item_uid{tail}
            ORDER BY r.rated_at DESC, i.title ASC LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    ).fetchall()

    # `my_*` are this query's own columns riding along beside the item's;
    # UnifiedItem forbids extras, so they come off before it sees the row.
    items = fill_from_siblings(
        conn,
        [
            UnifiedItem.from_row({k: v for k, v in dict(r).items() if not k.startswith("my_")})
            for r in rows
        ],
    )
    return int(total), [
        (float(r["my_rating"]), r["my_rated_at"], item) for r, item in zip(rows, items, strict=True)
    ]


def rated_uids(
    conn: sqlite3.Connection, *, user_uid: str = LOCAL_USER_UID
) -> dict[str, float]:
    """Every work you have rated, in listing-space uids, mapped to your score.

    Translated through ``links`` again on the way out -- not because
    :func:`set_rating` failed to, but because relinking moves representatives.
    A rating written before the linker last ran can name a row that has since
    lost the primary election, and the embedding matrix holds only winners
    (``text.work_documents`` selects ``is_primary = 1``). Left untranslated such
    a rating would contribute nothing to a taste profile and raise nothing --
    the same silent failure ``CollaborativeIndex.similar_to`` translates to
    avoid.

    Two ratings that drift onto one representative collapse to the later score,
    which is what re-rating a work means anyway.
    """
    from recsys.link import representative_uid

    rows = conn.execute(
        "SELECT item_uid, rating FROM ratings WHERE user_uid = ? ORDER BY rated_at", (user_uid,)
    ).fetchall()
    return {representative_uid(conn, r["item_uid"]): float(r["rating"]) for r in rows}


def start_ingest_run(conn: sqlite3.Connection, source: str, domain: str) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO ingest_runs (source, domain, started_at) VALUES (?, ?, ?)",
            (source, domain, datetime.now(UTC).isoformat()),
        )
    row_id = cur.lastrowid
    assert row_id is not None  # a successful INSERT always sets lastrowid
    return row_id


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
                datetime.now(UTC).isoformat(),
                n_fetched, n_inserted, n_updated, n_cached, status, error, run_id,
            ),
        )


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

# Duplicate collapsing. Two sources describing one work are two rows on purpose
# (`item_uid` is derived, and Phase 2 needs both to survive), so nothing is ever
# merged or deleted -- reads simply show one row per work. `links` names the
# winner; an item with no link row is a work of one and always shows.
#
# Deliberately not a source-preference list: ranking by source name here would
# mean editing db.py to add a source, which the project invariant forbids.
# `link.py` picks the richest row instead, by what the row actually contains.
_PRIMARY_JOIN = "LEFT JOIN links l ON l.item_uid = i.item_uid"
_PRIMARY_ONLY = "(l.item_uid IS NULL OR l.is_primary = 1)"

# Ordering uses `recsys.popularity`'s calibrated value, not the raw count: raw
# popularity means something different in every source. Falls back to the raw
# number when nothing has been calibrated yet (a fresh database, or --no-link),
# so ordering degrades to the old behaviour rather than collapsing.
_POP_JOIN = "LEFT JOIN popularity_norm pn ON pn.item_uid = i.item_uid"
_POP_ORDER = "COALESCE(pn.value, i.popularity)"

# `recsys.works` precomputes both of the above onto one row per work, which is
# what lets an index satisfy the ORDER BY instead of a temp b-tree. It is a
# cache, so reads fall back to the joins above when it has not been built --
# a fresh catalog, or a `--no-link` bootstrap, still lists its items rather
# than showing an empty shelf. Same contract as `links` and `popularity_norm`:
# rebuilt in bulk, never maintained incrementally, and stale until it is.
_WORKS_JOIN = "JOIN works w ON w.item_uid = i.item_uid"
_WORKS_ORDER = "w.sort_key"


def _works_built(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute("SELECT EXISTS(SELECT 1 FROM works)").fetchone()[0] == 1)


#: Reciprocal rank fusion's constant (Cormack, Clarke & Buettcher, SIGIR 2009):
#: a result's score is the sum of 1 / (k + rank) over the rankings fused. It
#: needs no shared scale between them -- bm25 and an audience count have none --
#: and k = 60 is the value that paper found robust, not one tuned here.
RRF_K = 60

#: How well a match fits the words typed, as a ranking. Search used bm25 over
#: title, alt titles and description, and bm25 rewards repetition: a special
#: whose synopsis says "Steins;Gate" three times outranked the series. A
#: title-only bm25 does not escape it either -- FTS5 normalises by the whole
#: row's length, so a short blurb still lifts a title. So: works whose *title*
#: holds every word typed come first, the fewer words the title has the better;
#: matches that touch no title follow, ordered by bm25 over the whole document
#: (lower is better). A dense rank, so a crowd of slightly shorter titles costs
#: a popular work one place, not one place per title.
_TEXT_ORDER = (
    "ORDER BY in_title DESC, CASE WHEN in_title THEN words ELSE 0 END, "
    "CASE WHEN in_title THEN 0 ELSE score END"
)


def title_tokens(text: str | None) -> list[str]:
    """A title as FTS5's tokenizer sees it: lower case, no diacritics, words only.

    ``unicode61 remove_diacritics 2`` folds case and strips accents, and splits
    on anything that is not a letter or digit -- so "Steins;Gate" is two words
    and "Pokémon" matches "pokemon". The same here, so a title can be compared
    with what was typed.
    """
    import unicodedata

    folded = unicodedata.normalize("NFKD", (text or "").casefold())
    stripped = "".join(c for c in folded if not unicodedata.combining(c))
    return _TOKEN_RE.findall(stripped)


def title_key(text: str | None) -> str:
    """``title_tokens`` joined: equal keys mean the same title, typed any way."""
    return " ".join(title_tokens(text))


def build_fts_query(q: str, column: str | None = None) -> str | None:
    """Turn free user text into a safe FTS5 MATCH expression.

    User input never reaches FTS5 as syntax: every token is quoted as a string
    literal, so ``AND`` / ``*`` / ``"`` / ``NEAR`` typed in the search box are
    treated as ordinary words. The final token gets a prefix wildcard so
    as-you-type search matches partial words. ``column`` confines every token
    to one column; it is a name from this module, never user input.
    """
    tokens = _TOKEN_RE.findall(q or "")
    if not tokens:
        return None
    quoted = [f'"{t}"' for t in tokens[:-1]]
    quoted.append(f'"{tokens[-1]}"*')
    if column is not None:
        quoted = [f"{column} : {t}" for t in quoted]
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

    With ``q``, the FTS index drives the query. A work whose title (or an
    alternative title) *is* what was typed leads; everything else is ordered
    by two rankings fused by reciprocal rank -- how well the title matches (the
    document, when no title does), and the calibrated popularity ranked within
    each domain. Neither alone is right: bm25 puts a special above its series,
    popularity alone ignores what was typed. The fusion is scale-free, so no
    weight between them has to be invented; ties go to the more popular.

    Without ``q`` and with a ``domain``, works are ordered by that domain's
    calibrated popularity. Without either, by the all-domains order
    ``works.py`` chose: works with a cover first, domains interleaved by each
    one's own popularity rank.

    One row per *work*: where several sources describe the same thing, only the
    row `link.py` marked primary is returned. Use :func:`get_item` to reach a
    non-primary row directly by uid -- it is still there.

    And one card per *book series*: a listing shows the series' most read
    volume, and a search shows the volume that matched best, once per series.
    Both need `works`; without it the volumes list individually.
    """
    params: list[Any] = []
    where: list[str] = []

    fast = _works_built(conn)
    if fast:
        # `works` already holds one row per work, so the primary-only predicate
        # is the join itself and the domain filter goes through the same index
        # as the ordering.
        joins, sort = _WORKS_JOIN, _WORKS_ORDER
        if domain:
            where.append("w.domain = ?")
    else:
        joins, sort = f"{_PRIMARY_JOIN} {_POP_JOIN}", _POP_ORDER
        if domain:
            where.append("i.domain = ?")
        where.append(_PRIMARY_ONLY)  # no placeholder, so param order is unaffected

    match = build_fts_query(q) if q else None
    if q and match is None:
        # e.g. q="!!!" -- nothing searchable in it at all
        return (0, [])

    if match:
        params.append(match)
        if domain:
            params.append(domain)
        tail = " AND " + " AND ".join(where) if where else ""
        series = "w.series_uid" if fast else "NULL"
        # Calibrated popularity: `popularity_norm`'s value, which `works.sort_key`
        # carries wherever one exists (a row with no audience count at all is
        # NULL either way, never a raw 0 outranking it). Ranked *within* each
        # domain: calibration puts one domain's sources on one scale, and a
        # Deezer fan count is not comparable with a MAL member count.
        pop = "CASE WHEN w.sort_key > 0 THEN w.sort_key END" if fast else "pn.value"
        dom = "w.domain" if fast else "i.domain"
        # The title as FTS5 sees it, from `works` when it is built.
        words = "w.title_tokens" if fast else (
            "length(trim(i.title)) - length(replace(trim(i.title), ' ', '')) + 1"
        )
        key = title_key(q)
        exact = (
            "(w.title_key = ? OR instr(w.alt_keys, ?) > 0)" if fast else "(0 AND ? AND ?)"
        )
        collection = "w.is_collection" if fast else "0"
        # One pass over the matches. Each series keeps its best match, then two
        # rankings are fused by reciprocal rank (see `RRF_K`), and the total is
        # a window over the same rows rather than a second scan -- every
        # keystroke of as-you-type search pays for this. The page is cut before
        # the join, so only its rows are read back from `items`. `rid` closes
        # every tie, so paging is stable.
        rows = conn.execute(
            f"""WITH titled AS (
                    SELECT rowid FROM items_fts WHERE items_fts MATCH ?),
                matched AS (
                    SELECT i.rowid AS rid, {series} AS series, {dom} AS dom, {pop} AS pop,
                           i.rowid IN (SELECT rowid FROM titled) AS in_title,
                           {exact} AS exact, {words} AS words, {collection} AS coll,
                           bm25(items_fts, 10.0, 4.0, 1.0) AS score
                      FROM items_fts f JOIN items i ON i.rowid = f.rowid {joins}
                     WHERE items_fts MATCH ?{tail}),
                best AS (
                    SELECT rid, dom, pop, in_title, exact, words, score FROM matched
                     WHERE series IS NULL
                    UNION ALL
                    -- A series' best match: an exact title, then a single
                    -- volume over a boxed set, then a volume whose title holds
                    -- the words, then the more read volume. Not the shorter
                    -- title: every volume's title carries the series' name,
                    -- and a word's difference between them means nothing.
                    SELECT rid, dom, pop, in_title, exact, words, score FROM (
                        SELECT rid, dom, pop, in_title, exact, words, score, ROW_NUMBER() OVER (
                                   PARTITION BY series ORDER BY exact DESC, coll,
                                   in_title DESC, pop DESC NULLS LAST,
                                   CASE WHEN in_title THEN 0 ELSE score END, rid) AS rn
                          FROM matched WHERE series IS NOT NULL)
                     WHERE rn = 1),
                ranked AS (
                    SELECT rid, pop, exact,
                           DENSE_RANK() OVER ({_TEXT_ORDER}) AS text_rank,
                           RANK() OVER (PARTITION BY dom ORDER BY pop DESC NULLS LAST)
                               AS pop_rank,
                           COUNT(*) OVER () AS total
                      FROM best),
                page AS (
                    SELECT rid, pop, exact, total, text_rank, pop_rank,
                           1.0 / ({RRF_K} + text_rank) + 1.0 / ({RRF_K} + pop_rank) AS fused
                      FROM ranked
                     ORDER BY exact DESC, fused DESC, pop_rank, text_rank, rid
                     LIMIT ? OFFSET ?)
                SELECT {_SELECT_COLS}, p.total AS total FROM page p
                  JOIN items i ON i.rowid = p.rid
                 ORDER BY p.exact DESC, p.fused DESC, p.pop_rank, p.text_rank, p.rid""",
            [build_fts_query(q or "", column="title"), key, f"|{key}|", *params,
             limit, offset],
        ).fetchall()
        if rows:
            total = rows[0]["total"]
        else:  # past the last page: the window had no row to report on
            card = "COALESCE(w.series_uid, w.item_uid)" if fast else "i.item_uid"
            total = conn.execute(
                f"""SELECT COUNT(DISTINCT {card}) FROM items_fts f
                    JOIN items i ON i.rowid = f.rowid {joins}
                    WHERE items_fts MATCH ?{tail}""",
                params,
            ).fetchone()[0]
        rows = [{k: v for k, v in dict(r).items() if k != "total"} for r in rows]
    else:
        if fast:
            where.insert(0, "w.listed = 1")
        base = f"FROM items i {joins}"
        if domain:
            params.append(domain)
        tail = " WHERE " + " AND ".join(where) if where else ""
        if fast and not domain:
            # All domains have no single popularity order; `browse_rank` is the
            # one `works.py` chose -- covers first, domains interleaved.
            order = "ORDER BY w.browse_rank ASC"
        else:
            # `w.title` rather than `i.title` when it can be: the same value,
            # and the one the index carries, so the whole ORDER BY is the index's.
            order = f"ORDER BY {sort} DESC, {'w' if fast else 'i'}.title ASC"
        # Counted straight off `works`, without touching `items` at all. The
        # page needs the item columns; the count needs one row per card, which
        # is what `works` *is* -- joining back to `items` to count them cost
        # 60 ms of the 62, an index probe per work to learn nothing.
        count_base = "FROM works w" if fast else base

        total = conn.execute(f"SELECT COUNT(*) {count_base}{tail}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} {base}{tail} {order} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    items = [UnifiedItem.from_row(r) for r in rows]
    return (int(total), fill_from_siblings(conn, items))


def series_sizes(conn: sqlite3.Connection, uids: Sequence[str]) -> dict[str, int]:
    """For each uid that is a series' card, how many volumes it stands for."""
    out: dict[str, int] = {}
    for chunk in _chunks(list(uids), 400):
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT item_uid, n_series FROM works "
            f"WHERE item_uid IN ({marks}) AND n_series > 1",
            list(chunk),
        ):
            out[r["item_uid"]] = int(r["n_series"])
    return out


def series_members(
    conn: sqlite3.Connection, item_uid: str
) -> tuple[str | None, list[UnifiedItem]] | None:
    """(series name, its volumes in order) for the series ``item_uid`` is in.

    ``None`` when it is in no series. Any row of a volume's work reaches it.
    """
    from recsys.link import representative_uid

    row = conn.execute(
        "SELECT series_uid, series_name FROM works WHERE item_uid = ?",
        (representative_uid(conn, item_uid),),
    ).fetchone()
    if row is None or row["series_uid"] is None:
        return None
    rows = conn.execute(
        f"""SELECT {_SELECT_COLS} FROM works w JOIN items i ON i.item_uid = w.item_uid
            WHERE w.series_uid = ?
            ORDER BY w.series_pos IS NULL, w.series_pos, i.title""",
        (row["series_uid"],),
    ).fetchall()
    items = fill_from_siblings(conn, [UnifiedItem.from_row(r) for r in rows])
    return (row["series_name"], items)


def group_peers(conn: sqlite3.Connection, uids: Iterable[str]) -> set[str]:
    """Every work sharing a group with any of ``uids``, themselves included.

    The group is the connected component of adaptation, franchise and series
    edges `works.py` stored. Uids `works` does not hold come back unchanged.
    """
    wanted = list(dict.fromkeys(uids))
    out = set(wanted)
    for chunk in _chunks(wanted, 400):
        marks = ",".join("?" * len(chunk))
        out.update(
            r[0]
            for r in conn.execute(
                f"""SELECT peer.item_uid FROM works mine
                    JOIN works peer ON peer.group_uid = mine.group_uid
                    WHERE mine.item_uid IN ({marks}) AND mine.group_uid IS NOT NULL""",
                list(chunk),
            )
        )
    return out


def groups_of(conn: sqlite3.Connection, uids: Sequence[str]) -> dict[str, str]:
    """uid -> its group, for the uids that are in one."""
    out: dict[str, str] = {}
    for chunk in _chunks(list(uids), 400):
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT item_uid, group_uid FROM works "
            f"WHERE item_uid IN ({marks}) AND group_uid IS NOT NULL",
            list(chunk),
        ):
            out[r["item_uid"]] = r["group_uid"]
    return out


#: Scalars a sibling row may supply when the representative lacks them. Lists
#: are deliberately absent: filling a null is unambiguous, but merging two
#: sources' tags is a modelling decision, and Phase 3 reads the real union
#: through `link.work_members()`.
_FILLABLE = ("year", "description", "popularity", "mean_rating", "n_ratings", "image_url")

#: Of those, the ones that count an audience. A work can span domains -- an
#: anime and its TMDB TV entry -- and a TMDB vote count or vote average is not
#: a reading of the anime's MAL audience, so these come only from rows filed
#: under the representative's own domain.
_SAME_DOMAIN_ONLY = frozenset({"popularity", "mean_rating", "n_ratings"})


def fill_from_siblings(
    conn: sqlite3.Connection, items: list[UnifiedItem]
) -> list[UnifiedItem]:
    """Complete each work's representative from the other rows describing it.

    No single source is complete. Open Library has a book's synopsis and none of
    its ratings; goodbooks has the ratings and no prose. The representative wins
    on having text, so without this a book would display with a synopsis and no
    score, year or cover -- worse than either row alone.

    Only nulls are filled, and only from rows of the same work, so nothing here
    can overwrite a source's own datum with another's.
    """
    gapped = [i for i in items if any(getattr(i, f) is None for f in _FILLABLE)]
    if not gapped:
        return items

    by_uid = {i.item_uid: i for i in gapped}
    donors: dict[str, list[sqlite3.Row]] = {}
    for chunk in _chunks(list(by_uid), 400):
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"""SELECT mine.item_uid AS target, {_SELECT_COLS}
                FROM links mine
                JOIN links theirs ON theirs.work_uid = mine.work_uid
                JOIN items i ON i.item_uid = theirs.item_uid
                WHERE mine.item_uid IN ({marks}) AND theirs.item_uid != mine.item_uid
                ORDER BY theirs.item_uid""",
            list(chunk),
        ):
            donors.setdefault(row["target"], []).append(row)

    filled: list[UnifiedItem] = []
    for item in items:
        rows = donors.get(item.item_uid)
        if not rows:
            filled.append(item)
            continue
        patch = {
            field: value
            for field in _FILLABLE
            if getattr(item, field) is None
            for value in (
                next(
                    (
                        r[field] for r in rows
                        if r[field] is not None
                        and (field not in _SAME_DOMAIN_ONLY or r["domain"] == item.domain)
                    ),
                    None,
                ),
            )
            if value is not None
        }
        filled.append(item.model_copy(update=patch) if patch else item)
    return filled


def get_item(conn: sqlite3.Connection, item_uid: str) -> UnifiedItem | None:
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM items i WHERE i.item_uid = ?", (item_uid,)
    ).fetchone()
    if row is None:
        return None
    return fill_from_siblings(conn, [UnifiedItem.from_row(row)])[0]


def count_items(conn: sqlite3.Connection) -> int:
    """Cards, not rows -- what a reader of the catalog actually sees.

    One per work, and one per book series rather than per volume once `works`
    is built. Before then the fallback counts works, volumes included: the
    series are a property of the read view, not of the rows.
    """
    if _works_built(conn):
        return int(conn.execute("SELECT COUNT(*) FROM works WHERE listed = 1").fetchone()[0])
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM items i {_PRIMARY_JOIN} WHERE {_PRIMARY_ONLY}"
        ).fetchone()[0]
    )


def count_rows(conn: sqlite3.Connection) -> int:
    """Stored rows, duplicates included. The operator's view, not the reader's."""
    return int(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])


def domain_stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    # n_items counts works; sources counts every source that contributed a row,
    # including ones whose rows all lost the primary election. A source that
    # supplied data is a source of the catalog whether or not it is displayed.
    if _works_built(conn):
        counted = {
            r["domain"]: r["n_items"]
            for r in conn.execute(
                "SELECT domain, COUNT(*) AS n_items FROM works WHERE listed = 1 "
                "GROUP BY domain"
            )
        }
        rows = [
            {"domain": r["domain"], "n_items": counted.get(r["domain"], 0),
             "sources": r["sources"]}
            for r in conn.execute(
                """SELECT domain, json_group_array(DISTINCT source) AS sources
                   FROM items GROUP BY domain"""
            )
        ]
        rows.sort(key=lambda r: (-r["n_items"], r["domain"]))
    else:
        rows = conn.execute(
            f"""SELECT domain,
                      SUM(CASE WHEN {_PRIMARY_ONLY} THEN 1 ELSE 0 END) AS n_items,
                      json_group_array(DISTINCT source) AS sources
               FROM items i {_PRIMARY_JOIN}
               GROUP BY domain ORDER BY n_items DESC"""
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
