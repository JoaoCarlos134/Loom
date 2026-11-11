"""Measure the story groups, read-only: with the crossover guard and without.

    uv run python scripts/groups.py              # the catalog in DATA_DIR
    uv run python scripts/groups.py --db PATH    # any copy of one

The numbers behind CLAUDE.md's "Same-franchise suppression" rule. Groups are
what results suppress for a work you rated or asked about, and what they
collapse to one card, so two franchises welded into one group hide each other
everywhere. This prints how many groups there are, the largest, and for a few
known pairs whether they share a group -- the crossovers should not, the
same-story pairs should. Opens the database read-only; writes nothing.

Measured 2026-11-11 on the full build (47,698 group edges + 2,976 series
edges): without the guard all four crossover pairs were joined, with it all
four apart and every same-story pair together; 342 two-parent works, 99 held.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recsys.config import get_settings
from recsys.relations import GROUP_RELATIONS, _series_edges
from recsys.works import _Components, story_components

#: Crossovers: two franchises a festival short or joint one-shot connects.
APART = [
    ("Dragon Ball Z", "ONE PIECE"),
    ("Nanatsu no Taizai", "FAIRY TAIL"),
    ("Kaguya-sama wa Kokurasetai: Tensaitachi no Renai Zunousen", "[Oshi no Ko]"),
    ("Nisekoi", "Bakemonogatari"),
    # Known miss, kept to notice if it ever changes: an anime adapting two
    # unrelated manga cannot be told from a manga with many adaptations.
    ("Hanma Baki", "Kengan Ashura"),
]
#: One story in different entries and media.
TOGETHER = [
    ("Shingeki no Kyojin", "Shingeki no Kyojin: The Final Season"),
    ("Steins;Gate", "Steins;Gate 0"),
    ("DEATH NOTE", "DEATH NOTE"),  # the anime and the manga
    ("Kimetsu no Yaiba", "Kimetsu no Yaiba"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, default=None, help="database (default: DATA_DIR's)")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    path = args.db or get_settings().db_path
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cap = get_settings().max_group_size

    rep_of = {r[0]: r[1] for r in conn.execute(
        "SELECT mine.item_uid, p.item_uid FROM links mine "
        "JOIN links p ON p.work_uid = mine.work_uid AND p.is_primary = 1")}
    works = {r[0] for r in conn.execute(
        "SELECT i.item_uid FROM items i LEFT JOIN links l ON l.item_uid = i.item_uid "
        "WHERE l.item_uid IS NULL OR l.is_primary = 1")}
    marks = ",".join("?" * len(GROUP_RELATIONS))
    edges = [(r[0], r[1], r[2]) for r in conn.execute(
        f"SELECT src_item_uid, dst_item_uid, relation FROM relations WHERE relation IN ({marks})",
        sorted(GROUP_RELATIONS))]
    n_relation = len(edges)
    edges += [(a, b, "SERIES") for a, b in _series_edges(conn)]
    print(f"{path}\n{n_relation} relation edges + {len(edges) - n_relation} series edges, "
          f"{len(works)} works, MAX_GROUP_SIZE {cap}\n")

    def uid_of(title: str, domain: str) -> str | None:
        row = conn.execute(
            "SELECT i.item_uid FROM items i LEFT JOIN links l ON l.item_uid = i.item_uid "
            "LEFT JOIN popularity_norm pn ON pn.item_uid = i.item_uid "
            "WHERE i.domain = ? AND i.title = ? COLLATE NOCASE "
            "AND (l.item_uid IS NULL OR l.is_primary = 1) "
            "ORDER BY pn.value DESC NULLS LAST LIMIT 1", (domain, title)).fetchone()
        return row[0] if row else None

    for guard in (False, True):
        comp, held = story_components(edges, rep_of, crossover_guard=guard)
        sizes = sorted((len(m) for m in comp.groups(works).values()), reverse=True)
        print(f"== crossover guard {'ON' if guard else 'OFF'}: {len(sizes)} groups of 2+, "
              f"largest {sizes[:5]}, over the cap {sum(1 for n in sizes if n > cap)}, "
              f"held as crossovers {len(held)}")

        def same(a: str | None, b: str | None, comp: _Components = comp) -> str:
            if a is None or b is None:
                return "not in catalog"
            return "same group" if comp.find(a) == comp.find(b) else "apart"

        for x, y in APART:
            print(f"   should be apart:    {x} / {y}: {same(uid_of(x, 'anime'), uid_of(y, 'anime'))}")
        for x, y in TOGETHER:
            ys = uid_of(y, "manga") if x == y else uid_of(y, "anime")
            print(f"   should be together: {x} / {y}: {same(uid_of(x, 'anime'), ys)}")
        print()


if __name__ == "__main__":
    main()
