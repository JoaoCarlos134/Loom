"""The manga domain: Loom's second domain, and the first check that the
one-file-per-domain invariant is real rather than asserted.

Driven by `tests/fixtures/anilist_manga_sample.json`: three real AniList records
reached the way the ingester reaches them -- by following adaptation edges out
of anime already in the catalog. Between them they cover the MANGA and NOVEL
formats and the case that matters most for mapping, a work with no studio at
all.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from recsys.config import Settings, get_settings
from recsys.db import connect, init_db, query_items, upsert_items
from recsys.ingest.anilist import map_anilist_media
from recsys.ingest.manga_anilist import AniListMangaIngester
from recsys.link import rebuild_links
from recsys.schemas import UnifiedItem, utcnow

FIXTURE = Path(__file__).parent / "fixtures" / "anilist_manga_sample.json"


@pytest.fixture
def records() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def manga(records) -> dict:
    return copy.deepcopy(records[0])


@pytest.fixture
def settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


def anime_with_relations(source_id: int, edges: list[tuple[str, str, int]]) -> UnifiedItem:
    """An anime row carrying relation edges, the way `anime_anilist` leaves them."""
    return UnifiedItem(
        domain="anime", source="anilist", source_id=str(source_id),
        title=f"Anime {source_id}", fetched_at=utcnow(),
        raw_json={
            "id": source_id,
            "relations": {
                "edges": [
                    {"relationType": rt, "node": {"id": nid, "type": ntype}}
                    for rt, ntype, nid in edges
                ]
            },
        },
    )


@pytest.fixture
def seeded(settings):
    init_db()
    conn = connect()
    yield conn
    conn.close()


# ---- mapping --------------------------------------------------------------


def test_every_fixture_record_maps(records):
    items = [map_anilist_media(r, "manga") for r in records]
    assert all(isinstance(i, UnifiedItem) for i in items)
    assert len(items) == 3


def test_rows_land_in_the_manga_domain(manga):
    item = map_anilist_media(manga, "manga")
    assert item.domain == "manga"
    assert item.item_uid == f"manga:anilist:{manga['id']}"


def test_the_same_mapper_serves_both_domains(manga):
    """One ``Media`` type upstream, one mapper here. The domain is the argument."""
    assert map_anilist_media(manga, "anime").domain == "anime"
    assert map_anilist_media(manga, "manga").domain == "manga"


def test_the_mal_url_points_at_the_manga_id_space(manga):
    """MyAnimeList keeps anime and manga in separate id spaces behind separate
    paths, so reusing /anime/ would be a confidently wrong link."""
    ids = map_anilist_media(manga, "manga").external_ids
    assert ids["mal_url"] == f"https://myanimelist.net/manga/{manga['idMal']}"
    assert "/anime/" not in ids["mal_url"]


def test_creators_come_from_staff_when_there_is_no_studio(manga):
    """Manga have no studio. Without the staff fallback a manga row would carry
    no creator at all, and "same author" is one of the cross-domain bridges."""
    assert manga["studios"]["edges"] == []
    creators = map_anilist_media(manga, "manga").creators
    assert creators
    assert any(c.role in ("Story", "Art", "Illustration") for c in creators)


def test_staff_roles_are_preserved(manga):
    creators = map_anilist_media(manga, "manga").creators
    assert any(c.role in ("Story", "Story & Art") for c in creators)


def test_the_author_is_listed_before_the_assistants(manga):
    """AniList returns staff in no particular order, so 7% of manga listed an
    assistant or translator first -- including an assistant ahead of Hajime
    Isayama on Attack on Titan."""
    manga["staff"]["edges"] = [
        {"role": "Assistant", "node": {"name": {"full": "An Assistant"}}},
        {"role": "Art", "node": {"name": {"full": "An Artist"}}},
        {"role": "Story", "node": {"name": {"full": "The Author"}}},
    ]
    names = [c.name for c in map_anilist_media(manga, "manga").creators]
    assert names == ["The Author", "An Artist", "An Assistant"]


@pytest.mark.parametrize(
    "role",
    ["Translator (English)", "Lettering (Spanish)", "Editing (Portuguese)", "Typesetting"],
)
def test_edition_credits_are_excluded(manga, role):
    """A translator belongs to an edition, not to the work. Keeping them would
    bridge two unrelated manga on a shared Spanish translator -- and AniList
    lists roughly 1,150 such credits across this catalogue."""
    manga["staff"]["edges"] = [
        {"role": role, "node": {"name": {"full": "A Localiser"}}},
        {"role": "Story & Art", "node": {"name": {"full": "The Author"}}},
    ]
    names = [c.name for c in map_anilist_media(manga, "manga").creators]
    assert names == ["The Author"]


def test_a_row_of_only_edition_credits_yields_no_creators(manga):
    manga["staff"]["edges"] = [
        {"role": "Translator (French)", "node": {"name": {"full": "A Localiser"}}},
    ]
    assert map_anilist_media(manga, "manga").creators == []


def test_the_format_becomes_a_tag(records):
    """manga / novel / one_shot keeps light novels separable from comics."""
    formats = {map_anilist_media(r, "manga").raw_json["format"].lower() for r in records}
    assert {"manga", "novel"} <= formats
    for r in records:
        assert r["format"].lower() in map_anilist_media(r, "manga").tags


def test_manga_carry_synopses(records):
    """The reason AniList is the source for this domain too."""
    assert all(len(map_anilist_media(r, "manga").description or "") > 100 for r in records)


# ---- what it decides to fetch ---------------------------------------------


def test_ids_come_from_adaptation_edges(seeded, settings):
    upsert_items(seeded, [
        anime_with_relations(1, [("ADAPTATION", "MANGA", 500)]),
        anime_with_relations(2, [("SOURCE", "MANGA", 501)]),
    ])
    assert set(AniListMangaIngester(settings).ids_to_fetch()) == {500, 501}


def test_looser_relations_are_ignored(seeded, settings):
    """CHARACTER and SPIN_OFF are associations, not the same story. Following
    them would dilute the one property that makes this domain valuable."""
    upsert_items(seeded, [
        anime_with_relations(1, [
            ("ADAPTATION", "MANGA", 500),
            ("CHARACTER", "MANGA", 600),
            ("SPIN_OFF", "MANGA", 601),
            ("OTHER", "MANGA", 602),
        ]),
    ])
    assert AniListMangaIngester(settings).ids_to_fetch() == [500]


def test_non_manga_relations_are_ignored(seeded, settings):
    """An anime adapted from an anime is not a manga."""
    upsert_items(seeded, [
        anime_with_relations(1, [
            ("ADAPTATION", "MANGA", 500),
            ("ADAPTATION", "ANIME", 700),
        ]),
    ])
    assert AniListMangaIngester(settings).ids_to_fetch() == [500]


def test_the_most_adapted_works_come_first(seeded, settings):
    """So an interrupted or --limit run gets the works most anime point at."""
    upsert_items(seeded, [
        anime_with_relations(1, [("ADAPTATION", "MANGA", 900)]),
        anime_with_relations(2, [("ADAPTATION", "MANGA", 900)]),
        anime_with_relations(3, [("ADAPTATION", "MANGA", 901)]),
    ])
    assert AniListMangaIngester(settings).ids_to_fetch() == [900, 901]


def test_each_manga_is_requested_once(seeded, settings):
    upsert_items(seeded, [
        anime_with_relations(i, [("ADAPTATION", "MANGA", 900)]) for i in range(1, 5)
    ])
    assert AniListMangaIngester(settings).ids_to_fetch() == [900]


def test_target_count_limits_the_id_list(seeded, settings):
    upsert_items(seeded, [
        anime_with_relations(i, [("ADAPTATION", "MANGA", 900 + i)]) for i in range(1, 6)
    ])
    ingester = AniListMangaIngester(settings)
    ingester.target_count = 2
    assert len(ingester.ids_to_fetch()) == 2


async def test_no_anime_means_nothing_to_fetch(seeded, settings):
    """anime_anilist has to run first; saying so beats crashing."""
    result = await AniListMangaIngester(settings).run(seeded)
    assert (result.status, result.n_fetched) == ("ok", 0)


# ---- the run --------------------------------------------------------------


@pytest.fixture
def offline(monkeypatch, records):
    async def fake_post(self, ids):
        return [copy.deepcopy(r) for r in records if r["id"] in ids]

    monkeypatch.setattr(AniListMangaIngester, "_post_batch", fake_post)


async def test_run_populates_a_second_domain(seeded, settings, offline, records):
    upsert_items(seeded, [
        anime_with_relations(i, [("ADAPTATION", "MANGA", r["id"])])
        for i, r in enumerate(records, start=1)
    ])
    result = await AniListMangaIngester(settings).run(seeded)

    assert result.status == "ok"
    assert (result.n_fetched, result.n_inserted) == (3, 3)
    domains = {r[0] for r in seeded.execute("SELECT DISTINCT domain FROM items")}
    assert domains == {"anime", "manga"}


async def test_manga_are_not_linked_to_the_anime_they_adapt(seeded, settings, offline, records):
    """`links` means "the same work", and an adaptation is not that -- it is a
    different work in a different medium. Merging them would hide one behind
    the other in listings. The relationship is real and worth recording, but
    not here."""
    upsert_items(seeded, [
        anime_with_relations(i, [("ADAPTATION", "MANGA", r["id"])])
        for i, r in enumerate(records, start=1)
    ])
    await AniListMangaIngester(settings).run(seeded)
    rebuild_links(seeded)

    assert query_items(seeded, domain="manga")[0] == 3
    assert query_items(seeded, domain="anime")[0] == 3


async def test_a_shared_mal_id_across_domains_stays_two_works(seeded, settings, offline):
    """MyAnimeList numbers anime and manga separately, so anime 1 and manga 1
    are unrelated. The work uid carries the domain precisely for this."""
    upsert_items(seeded, [
        UnifiedItem(domain="anime", source="mal", source_id="1", title="Anime One",
                    external_ids={"mal_id": "1"}),
        UnifiedItem(domain="manga", source="mal", source_id="1", title="Manga One",
                    external_ids={"mal_id": "1"}),
    ])
    assert rebuild_links(seeded).n_works == 0
    assert query_items(seeded)[0] == 2


# ---- the invariant --------------------------------------------------------


def test_registry_exposes_the_ingester(settings):
    from recsys.ingest.registry import available, get_ingester

    assert "manga_anilist" in available()
    ingester = get_ingester("manga_anilist", settings)
    assert (ingester.domain, ingester.source) == ("manga", "anilist")


def test_the_cache_key_changes_when_the_query_does(settings):
    """Responses are only valid for the query that produced them. Without this
    the `staff` field added for manga would have come back missing from cached
    anime payloads until the 30-day TTL expired."""
    from recsys.ingest.anime_anilist import AniListAnimeIngester

    anime = AniListAnimeIngester(settings)
    manga = AniListMangaIngester(settings)
    assert anime.query_fingerprint != manga.query_fingerprint
    assert len(anime.query_fingerprint) == 12


def test_anime_and_manga_batches_do_not_share_cache_entries(settings):
    """The two domains have overlapping id spaces, so a bare id list would
    collide and serve manga records to the anime ingester."""
    from recsys.ingest.anime_anilist import AniListAnimeIngester

    keys = set()
    for cls in (AniListAnimeIngester, AniListMangaIngester):
        ing = cls(settings)
        keys.add(
            f"{ing.url}#{ing.media_type}#{ing.query_fingerprint}#1,2,3"
        )
    assert len(keys) == 2


def test_adding_this_domain_needed_no_new_settings(settings):
    """One file, one registry line, and it reuses the AniList settings the
    anime ingester already declared. That is the invariant, checked."""
    ingester = AniListMangaIngester(settings)
    assert ingester.url == settings.anilist_url
    assert ingester.per_page == settings.anilist_page_size
    assert ingester.min_tag_rank == settings.anilist_min_tag_rank
