"""The derived tables, rebuilt in one call and in the right order."""

from __future__ import annotations

from recsys.db import connect, init_db, upsert_items
from recsys.derive import rebuild_derived
from recsys.schemas import UnifiedItem


def test_one_call_leaves_every_derived_table_consistent(tmp_path):
    """`works` stores groups made from `relations`; built in the wrong order a
    single call would leave every work ungrouped until the next one."""
    init_db(tmp_path / "d.db")
    conn = connect(tmp_path / "d.db")
    upsert_items(conn, [
        UnifiedItem(domain="anime", source="anilist", source_id="1", title="S1",
                    external_ids={"mal_id": "1"}, popularity=10.0,
                    raw_json={"relations": {"edges": [
                        {"relationType": "SEQUEL", "node": {"id": 2, "type": "ANIME"}}]}}),
        UnifiedItem(domain="anime", source="mal", source_id="1", title="S1",
                    external_ids={"mal_id": "1"}, popularity=12.0),
        UnifiedItem(domain="anime", source="anilist", source_id="2", title="S2",
                    popularity=5.0),
    ])
    derived = rebuild_derived(conn)
    assert derived.links.n_works == 1
    assert derived.relations.n_edges == 1
    assert derived.works.n_groups == 1
    groups = {r[0] for r in conn.execute("SELECT group_uid FROM works")}
    assert len(groups) == 1 and None not in groups
    conn.close()
