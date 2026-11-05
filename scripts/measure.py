"""Measure a catalog: the numbers the README and ROADMAP quote, from the database.

Read-only. The database is opened with ``mode=ro``, nothing is written, and no
model is rebuilt -- so it is safe to point at the build you use every day:

    uv run python scripts/measure.py                    # DATA_DIR from .env
    uv run python scripts/measure.py --db path/to/recsys.db
    uv run python scripts/measure.py --json > numbers.json

Every number in the docs should be one this prints, not one edited by hand.
A catalog built by an older version of Loom is measured as far as its schema
allows; sections that need a newer table say so instead of failing.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from recsys.config import get_settings

#: Searches the bug reports named, and the work each should put first.
SEARCHES = {
    "Fullmetal Alchemist": "Fullmetal Alchemist: Brotherhood",
    "Steins;Gate": "Steins;Gate",
}


def open_ro(path: Path) -> sqlite3.Connection:
    """A connection that cannot write, whatever the code below does."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    return conn.execute(sql, params).fetchone()[0]


def catalog(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {
        "schema_version": scalar(conn, "PRAGMA user_version"),
        "rows": scalar(conn, "SELECT COUNT(*) FROM items"),
        "rows_by_source": {
            f"{r['domain']}:{r['source']}": r["n"]
            for r in conn.execute(
                "SELECT domain, source, COUNT(*) AS n FROM items GROUP BY 1, 2 ORDER BY 3 DESC"
            )
        },
    }
    if has_table(conn, "works") and scalar(conn, "SELECT COUNT(*) FROM works"):
        out["works"] = scalar(conn, "SELECT COUNT(*) FROM works")
        out["works_by_domain"] = {
            r["domain"]: r["n"]
            for r in conn.execute(
                "SELECT domain, COUNT(*) AS n FROM works GROUP BY 1 ORDER BY 2 DESC"
            )
        }
        if has_column(conn, "works", "listed"):
            out["cards"] = scalar(conn, "SELECT COUNT(*) FROM works WHERE listed = 1")
        with_text = scalar(
            conn,
            """SELECT COUNT(*) FROM works w WHERE EXISTS (
                   SELECT 1 FROM items i WHERE i.item_uid = w.item_uid
                      AND COALESCE(TRIM(i.description), '') != '')
               OR EXISTS (
                   SELECT 1 FROM links a JOIN links b ON b.work_uid = a.work_uid
                     JOIN items s ON s.item_uid = b.item_uid
                    WHERE a.item_uid = w.item_uid AND COALESCE(TRIM(s.description), '') != '')""",
        )
        out["synopsis_coverage"] = round(with_text / out["works"], 4)
    if has_table(conn, "relations"):
        out["relations"] = {
            r["relation"]: r["n"]
            for r in conn.execute(
                "SELECT relation, COUNT(*) AS n FROM relations GROUP BY 1 ORDER BY 2 DESC"
            )
        }
    out["ratings"] = {
        r["ns"]: r["n"]
        for r in conn.execute(
            """SELECT substr(user_uid, 1, instr(user_uid, ':') - 1) AS ns, COUNT(*) AS n
               FROM ratings GROUP BY 1 ORDER BY 2 DESC"""
        )
    }
    return out


def twins(conn: sqlite3.Connection) -> dict[str, Any]:
    """Bug 1: TMDB entries whose title is exactly an anime's, and how many are linked."""
    pairs = conn.execute(
        """SELECT DISTINCT t.item_uid AS tmdb, a.item_uid AS anime
           FROM items t JOIN items a ON lower(a.title) = lower(t.title)
           WHERE t.source = 'tmdb' AND t.domain IN ('tv', 'movie') AND a.domain = 'anime'"""
    ).fetchall()
    tmdb_rows = {r["tmdb"] for r in pairs}
    linked = {
        r[0]
        for r in conn.execute(
            """SELECT l.item_uid FROM links l
               JOIN items i ON i.item_uid = l.item_uid
               WHERE i.source = 'tmdb' AND l.work_uid LIKE 'work:anime:%'"""
        )
    }
    out: dict[str, Any] = {
        "tmdb_rows_titled_like_an_anime": len(tmdb_rows),
        "of_those_linked_into_an_anime_work": len(tmdb_rows & linked),
        "tmdb_rows_linked_into_an_anime_work": len(linked),
    }
    if has_table(conn, "id_map"):
        out["id_map_claims"] = scalar(conn, "SELECT COUNT(*) FROM id_map")
    return out


def groups(conn: sqlite3.Connection) -> dict[str, Any]:
    """Bug 2: groups results collapse to, and the book series listings fold."""
    if not (has_table(conn, "works") and has_column(conn, "works", "group_uid")):
        return {"skipped": "works has no group_uid; rebuild with bootstrap.py --no-ingest"}
    sizes = [
        (r["n"], r["group_uid"], r["title"])
        for r in conn.execute(
            """SELECT w.group_uid, COUNT(*) AS n,
                      (SELECT title FROM works g WHERE g.item_uid = w.group_uid) AS title
               FROM works w WHERE w.group_uid IS NOT NULL
               GROUP BY w.group_uid ORDER BY n DESC"""
        )
    ]
    series = conn.execute(
        """SELECT COUNT(DISTINCT series_uid) AS n_series, COUNT(*) AS n_volumes,
                  SUM(listed = 0) AS n_folded
           FROM works WHERE series_uid IS NOT NULL"""
    ).fetchone()
    return {
        "groups": len(sizes),
        "works_in_groups": sum(n for n, _, _ in sizes),
        "largest_groups": [{"size": n, "uid": g, "title": t} for n, g, t in sizes[:10]],
        "book_series": series["n_series"],
        "book_volumes_in_series": series["n_volumes"],
        "volumes_folded_behind_a_card": series["n_folded"],
        "death_note_book_cards": scalar(
            conn,
            """SELECT COUNT(*) FROM works
               WHERE domain = 'book' AND listed = 1 AND title LIKE '%Death Note%'""",
        ),
    }


def searches(conn: sqlite3.Connection) -> dict[str, Any]:
    """Bug 3: where the work people mean lands in a search for it."""
    from recsys.db import query_items

    out: dict[str, Any] = {}
    for q, want in SEARCHES.items():
        try:
            _, items = query_items(conn, q=q, limit=10)
        except sqlite3.Error as exc:  # an older schema the current code cannot read
            out[q] = {"error": str(exc)}
            continue
        titles = [i.title for i in items]
        out[q] = {
            "rank_of_" + want: (titles.index(want) + 1) if want in titles else None,
            "top_5": titles[:5],
        }
    return out


def covers(conn: sqlite3.Connection) -> dict[str, Any]:
    """Bug 4: works with no image, by domain, counting what siblings can lend."""
    has_image = """(
        (SELECT image_url FROM items WHERE item_uid = w.item_uid) IS NOT NULL
        OR EXISTS (SELECT 1 FROM links a JOIN links b ON b.work_uid = a.work_uid
                   JOIN items s ON s.item_uid = b.item_uid
                   WHERE a.item_uid = w.item_uid AND s.image_url IS NOT NULL))"""
    out: dict[str, Any] = {
        "rows_without_image_by_source": {
            f"{r['domain']}:{r['source']}": f"{r['missing']} of {r['n']}"
            for r in conn.execute(
                """SELECT domain, source, COUNT(*) AS n,
                          SUM(image_url IS NULL OR image_url LIKE '%/nophoto/%') AS missing
                   FROM items GROUP BY 1, 2 ORDER BY 1, 2"""
            )
        },
        "goodreads_placeholders_stored": scalar(
            conn, "SELECT COUNT(*) FROM items WHERE image_url LIKE '%/nophoto/%'"
        ),
    }
    if has_table(conn, "works") and scalar(conn, "SELECT COUNT(*) FROM works"):
        listed = "w.listed = 1" if has_column(conn, "works", "listed") else "1"
        if has_column(conn, "works", "has_image"):
            has_image = "w.has_image"
        out["works_without_image_by_domain"] = {
            r["domain"]: f"{r['missing']} of {r['n']}"
            for r in conn.execute(
                f"""SELECT w.domain, COUNT(*) AS n, SUM(NOT {has_image}) AS missing
                    FROM works w WHERE {listed} GROUP BY 1 ORDER BY 1"""
            )
        }
    from recsys.db import query_items

    try:
        _, first_page = query_items(conn, limit=40)
        out["default_view_first_40"] = {
            "with_image": sum(1 for i in first_page if i.image_url),
            "domains": sorted({i.domain for i in first_page}),
        }
    except sqlite3.Error as exc:
        out["default_view_first_40"] = {"error": str(exc)}
    return out


def tests() -> int | None:
    """How many tests the suite collects, so the docs can quote it."""
    import subprocess

    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    for line in done.stdout.splitlines()[::-1]:
        if " test" in line and ("collected" in line or "selected" in line):
            return int(line.split()[0])
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure a Loom catalog, read-only")
    parser.add_argument("--db", type=Path, default=None, help="default: DATA_DIR/recsys.db")
    parser.add_argument("--json", action="store_true", help="print one JSON document")
    parser.add_argument("--no-tests", action="store_true", help="skip counting the tests")
    args = parser.parse_args(argv)

    path = args.db or get_settings().db_path
    if not path.is_file():
        print(f"no database at {path}", file=sys.stderr)
        return 1
    conn = open_ro(path)
    report = {
        "database": str(path),
        "catalog": catalog(conn),
        "twins": twins(conn),
        "groups": groups(conn),
        "search": searches(conn),
        "covers": covers(conn),
    }
    if not args.no_tests:
        report["tests"] = tests()
    conn.close()

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        for section, body in report.items():
            print(f"\n== {section}")
            print(json.dumps(body, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
