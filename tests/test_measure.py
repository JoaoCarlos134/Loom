"""scripts/measure.py: the numbers the docs quote, read from a database it never writes."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

from recsys.db import connect, init_db, upsert_items
from recsys.derive import rebuild_derived
from recsys.schemas import UnifiedItem

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure.py"


def load():
    spec = importlib.util.spec_from_file_location("measure", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_measures_without_writing_a_byte(tmp_path, capsys):
    db = tmp_path / "recsys.db"
    init_db(db)
    conn = connect(db)
    upsert_items(conn, [
        UnifiedItem(domain="anime", source="anilist", source_id="9253", title="Steins;Gate",
                    description="A lab.", popularity=10.0, image_url="https://x/1.jpg",
                    external_ids={"mal_id": "9253"}),
        UnifiedItem(domain="tv", source="tmdb", source_id="42509", title="Steins;Gate",
                    external_ids={"tmdb_id": "42509"}),
        UnifiedItem(domain="book", source="goodbooks", source_id="1",
                    title="Death Note, Vol. 1 (Death Note, #1)", popularity=5.0),
        UnifiedItem(domain="book", source="goodbooks", source_id="2",
                    title="Death Note, Vol. 2 (Death Note, #2)", popularity=4.0),
    ])
    rebuild_derived(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    assert load().main(["--db", str(db), "--json", "--no-tests"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert report["catalog"]["rows"] == 4
    assert report["catalog"]["cards"] == 3  # no id map loaded: the twin is its own card
    assert report["twins"]["tmdb_rows_titled_like_an_anime"] == 1
    assert report["groups"]["book_series"] == 1
    assert report["search"]["Steins;Gate"]["rank_of_Steins;Gate"] == 1
    assert report["covers"]["default_view_first_40"]["with_image"] == 1


def test_a_missing_database_is_an_error_not_a_new_file(tmp_path):
    assert load().main(["--db", str(tmp_path / "none.db"), "--no-tests"]) == 1
    assert not (tmp_path / "none.db").exists()
