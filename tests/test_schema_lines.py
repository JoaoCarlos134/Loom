"""A database built by the other line of work still opens.

Two lines diverged at migration 6 and each shipped its own migration 7. The
1.0 line's is `id_map`; the other's added filter columns to `works` and the
`work_tags` / `tag_counts` caches -- which is migration 11 here. A database
at the other line's 7 would otherwise skip `id_map` and die creating
`work_tags` a second time. The owner's full build is one of those.
"""

from __future__ import annotations

import sqlite3

from recsys.db import MIGRATION_11, MIGRATIONS, SCHEMA_VERSION, init_db


def other_line_v7(path):
    conn = sqlite3.connect(path)
    for _, script in MIGRATIONS[:6]:
        conn.executescript(script)
    conn.executescript(MIGRATION_11)  # what that line called migration 7
    conn.execute("PRAGMA user_version = 7")
    conn.execute(
        "INSERT INTO ratings VALUES ('local:you', 'anime:mal:1535', 9.0, '2026-11-10')"
    )
    conn.commit()
    conn.close()


def test_the_other_lines_migration_7_upgrades_to_the_current_schema(tmp_path):
    path = tmp_path / "old.db"
    other_line_v7(path)

    assert init_db(path) == SCHEMA_VERSION

    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"id_map", "work_tags", "tag_counts"} <= tables
    columns = {r[1] for r in conn.execute("PRAGMA table_info(works)")}
    assert {"group_uid", "browse_rank", "title_key", "rating_key"} <= columns


def test_your_ratings_survive_it(tmp_path):
    path = tmp_path / "old.db"
    other_line_v7(path)
    init_db(path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT item_uid, rating FROM ratings").fetchall() == [
        ("anime:mal:1535", 9.0)
    ]


def test_this_lines_own_migration_7_is_left_alone(tmp_path):
    path = tmp_path / "new.db"
    init_db(path)
    assert init_db(path) == SCHEMA_VERSION
