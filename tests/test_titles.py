"""An English title beside the original one, so the UI can show either.

AniList names a work by its romanised original title -- *Kimetsu no Yaiba*,
and for a Korean webtoon *Na Honjaman Level Up* -- and stores the English one
(*Demon Slayer: Kimetsu no Yaiba*, *Solo Leveling*) among the alt titles,
where nothing can tell it from a Polish or Arabic one. The MAL dump has a
``title_english`` column. ``title_english`` keeps it, and only when it says
something the title does not.
"""

from __future__ import annotations

import sqlite3

from recsys.db import SCHEMA_VERSION, fill_from_siblings, get_item, init_db, upsert_items
from recsys.ingest.anilist import map_anilist_media
from recsys.ingest.anime_mal_csv import map_mal_row
from recsys.remap import REMAPPABLE, remap
from recsys.schemas import UnifiedItem, utcnow


def anilist(source_id, romaji, english, domain="manga"):
    return map_anilist_media({
        "id": source_id, "title": {"romaji": romaji, "english": english, "native": "x"},
        "synonyms": [],
    }, domain=domain)


def test_anilist_keeps_the_english_title():
    item = anilist(105398, "Na Honjaman Level Up", "Solo Leveling")
    assert item.title == "Na Honjaman Level Up"
    assert item.title_english == "Solo Leveling"


def test_an_english_title_that_is_the_title_is_not_repeated():
    assert anilist(1, "Monster", "Monster").title_english is None
    assert anilist(2, "Berserk", None).title_english is None


def test_the_mal_dump_keeps_its_english_column():
    item = map_mal_row({"anime_id": "38000", "title": "Kimetsu no Yaiba",
                        "title_english": "Demon Slayer: Kimetsu no Yaiba"})
    assert item.title_english == "Demon Slayer: Kimetsu no Yaiba"
    assert map_mal_row({"anime_id": "1", "title": "Cowboy Bebop",
                        "title_english": "Cowboy Bebop"}).title_english is None


def test_it_is_stored_and_read_back(tmp_path):
    init_db(tmp_path / "t.db")
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    upsert_items(conn, [anilist(105398, "Na Honjaman Level Up", "Solo Leveling")])
    assert get_item(conn, "manga:anilist:105398").title_english == "Solo Leveling"
    assert SCHEMA_VERSION >= 12


def test_a_work_borrows_it_from_a_sibling(tmp_path):
    """The representative may be the row without it; the card should not."""
    init_db(tmp_path / "t.db")
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    plain = UnifiedItem(domain="anime", source="anilist", source_id="1",
                        title="Kimetsu no Yaiba", description="text", fetched_at=utcnow())
    other = UnifiedItem(domain="anime", source="mal", source_id="38000",
                        title="Kimetsu no Yaiba", title_english="Demon Slayer",
                        fetched_at=utcnow())
    upsert_items(conn, [plain, other])
    conn.executemany("INSERT INTO links VALUES (?, ?, ?)",
                     [(plain.item_uid, "w", 1), (other.item_uid, "w", 0)])
    assert fill_from_siblings(conn, [plain])[0].title_english == "Demon Slayer"


def test_a_stored_catalog_is_backfilled_without_refetching(tmp_path):
    """Rows stored before the column existed get it from their own payloads."""
    init_db(tmp_path / "t.db")
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    upsert_items(conn, [anilist(105398, "Na Honjaman Level Up", "Solo Leveling")])
    conn.execute("UPDATE items SET title_english = NULL")

    class Manga:
        domain, source = "manga", "anilist"

        @staticmethod
        def map_item(raw):
            return map_anilist_media(raw, domain="manga")

    assert "title_english" in REMAPPABLE
    remap(conn, Manga(), ["title_english"])
    assert get_item(conn, "manga:anilist:105398").title_english == "Solo Leveling"
