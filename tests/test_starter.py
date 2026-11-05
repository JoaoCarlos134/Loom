"""The starter pack: built only from shareable data, installed only if it verifies.

A pack is built from a catalog holding every kind of row -- shippable books,
and the TMDB, AniList and MAL rows whose terms forbid redistribution -- so the
tests can say that the forbidden ones never reach the archive. No network:
URLs go through an ``httpx.MockTransport``.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from recsys.db import connect, init_db, set_rating, upsert_items, upsert_ratings
from recsys.derive import rebuild_derived
from recsys.schemas import UnifiedItem
from recsys.starter import (
    PACK_FILES,
    StarterError,
    build_pack,
    install_pack,
    sha256_of,
)


def row(domain, source, sid, title, **kw):
    return UnifiedItem(domain=domain, source=source, source_id=str(sid), title=title, **kw)


SOURCE_ROWS = [
    row("book", "goodbooks", 1, "Dark Matter", external_ids={"isbn": "1"}, popularity=900.0,
        image_url="https://images.gr-assets.com/books/1m/1.jpg"),
    row("book", "openlibrary", "1", "Dark Matter", description="A physicist wakes.",
        external_ids={"isbn": "1"}),
    row("book", "goodbooks", 2, "Neuromancer", popularity=800.0),
    row("book", "goodbooks", 3, "Never Let Me Go", popularity=700.0),
    row("anime", "anilist", 9253, "Steins;Gate", description="A lab."),
    row("anime", "mal", 9253, "Steins;Gate", external_ids={"mal_id": "9253"}),
    row("tv", "tmdb", 42509, "Steins;Gate", description="TMDB's words."),
    row("music", "musicbrainz", "mb1", "Hans Zimmer"),
]


MODEL = "fake/model"


def write_matrix(path, docs):
    """A matrix fingerprinted as the real build writes it; ``docs`` uid -> document."""
    from recsys.models.text import doc_fingerprint

    uids = list(docs)
    rng = np.random.default_rng(0)
    block = rng.normal(size=(len(uids), 4)).astype("float32")
    block /= np.linalg.norm(block, axis=1, keepdims=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({
        "item_uid": uids, "domain": [u.split(":")[0] for u in uids],
        "vector": pa.FixedSizeListArray.from_arrays(pa.array(block.reshape(-1)), 4),
        "doc_hash": [doc_fingerprint(docs[u], MODEL) for u in uids],
    }), path)


@pytest.fixture
def source(tmp_path):
    """A 'full build': shippable and unshippable rows, ratings of three kinds."""
    db = tmp_path / "full" / "recsys.db"
    init_db(db)
    conn = connect(db)
    upsert_items(conn, SOURCE_ROWS)
    rebuild_derived(conn)
    upsert_ratings(conn, [
        (f"goodbooks:{u}", f"book:goodbooks:{b}", float(r), None)
        for u in range(1, 6) for b, r in ((1, 8), (2, 6 + u % 2), (3, 4))
    ] + [("mal:someone", "anime:mal:9253", 9.0, None)])
    set_rating(conn, "book:goodbooks:2", 10)  # yours: must never ship
    from recsys.models.text import work_documents

    docs = {uid: doc for uid, _, doc in work_documents(conn)}
    conn.close()
    write_matrix(tmp_path / "full" / "features" / "embeddings.parquet", docs)
    return tmp_path / "full"


def cf(conn, out_path):
    from recsys.models.collaborative import build_collaborative

    build_collaborative(conn, out_path=out_path, min_raters=1, k=5)


@pytest.fixture
def pack(source, tmp_path):
    before = sha256_of(source / "recsys.db")
    result = build_pack(
        source / "recsys.db", embeddings=source / "features" / "embeddings.parquet",
        out_dir=tmp_path / "out", version="test", rebuild_derived=rebuild_derived,
        build_collaborative=cf, model=MODEL,
    )
    assert sha256_of(source / "recsys.db") == before  # the source was only read
    return result


def unpacked(pack, tmp_path) -> Path:
    where = tmp_path / "unzipped"
    with zipfile.ZipFile(pack.archive) as zf:
        zf.extractall(where)
    return where


# ---- building -------------------------------------------------------------


def test_only_shippable_rows_reach_the_pack(pack, tmp_path):
    db = unpacked(pack, tmp_path) / "recsys.db"
    conn = connect(db)
    sources = {(r[0], r[1]) for r in conn.execute("SELECT DISTINCT domain, source FROM items")}
    assert sources == {("book", "goodbooks"), ("book", "openlibrary")}
    assert conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == 0
    conn.close()


def test_the_pack_is_a_working_catalog_not_a_dump(pack, tmp_path):
    """Links, calibration and the read view are rebuilt for the pack itself."""
    conn = connect(unpacked(pack, tmp_path) / "recsys.db")
    assert conn.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 2
    conn.close()


def test_vectors_ship_only_for_the_packs_works(pack, tmp_path):
    table = pq.read_table(unpacked(pack, tmp_path) / "features" / "embeddings.parquet")
    assert sorted(set(table.column("domain").to_pylist())) == ["book"]
    assert pack.missing_vectors == 0


def test_the_collaborative_model_learns_from_shareable_ratings_only(pack, tmp_path):
    table = pq.read_table(unpacked(pack, tmp_path) / "features" / "collaborative.parquet")
    uids = set(table.column("item_uid").to_pylist()) | set(table.column("neighbour_uid").to_pylist())
    assert uids and all(u.startswith("book:goodbooks:") for u in uids)


def test_the_manifest_and_attribution_travel_with_it(pack, tmp_path):
    where = unpacked(pack, tmp_path)
    manifest = json.loads((where / "MANIFEST.json").read_text(encoding="utf-8"))
    for name, meta in manifest["files"].items():
        assert sha256_of(where / name) == meta["sha256"]
    attribution = (where / "ATTRIBUTION.md").read_text(encoding="utf-8")
    assert "CC BY-SA 4.0" in attribution and "Open Library" in attribution
    assert pack.sha256 == sha256_of(pack.archive)
    sidecar = Path(f"{pack.archive}.sha256").read_text(encoding="utf-8")
    assert sidecar.split()[0] == pack.sha256


# ---- installing -----------------------------------------------------------


def test_a_verified_pack_installs_and_serves(pack, tmp_path):
    from fastapi.testclient import TestClient

    from recsys.api.routes import _load_similarity_index
    from recsys.config import get_settings

    data = get_settings().data_dir
    result = install_pack(str(pack.archive), data)
    assert result.manifest["pack"] == "test"
    _load_similarity_index.cache_clear()
    from recsys.api.server import create_app

    with TestClient(create_app()) as client:
        assert client.get("/api/items").json()["total"] == 3
        assert client.get("/api/similar/book:goodbooks:2").status_code == 200
    _load_similarity_index.cache_clear()


def test_a_corrupt_download_is_refused_and_nothing_changes(pack, tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(pack.archive.read_bytes()[:-10] + b"0123456789")
    with pytest.raises(StarterError, match="checksum"):
        install_pack(str(bad), tmp_path / "data", sha256=pack.sha256)
    assert not (tmp_path / "data" / "recsys.db").exists()


def tampered(pack, tmp_path, extra: dict[str, bytes]) -> tuple[Path, str]:
    out = tmp_path / "tampered.zip"
    with zipfile.ZipFile(pack.archive) as src, zipfile.ZipFile(out, "w") as dst:
        for name in src.namelist():
            dst.writestr(name, extra.pop(name, src.read(name)))
        for name, body in extra.items():
            dst.writestr(name, body)
    return out, hashlib.sha256(out.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", ["../escape.txt", "extra.txt", "/abs.txt"])
def test_an_archive_with_anything_unexpected_is_refused(pack, tmp_path, name):
    archive, digest = tampered(pack, tmp_path, {name: b"x"})
    with pytest.raises(StarterError, match="not a Loom starter pack"):
        install_pack(str(archive), tmp_path / "data", sha256=digest)
    assert not (tmp_path / "data" / "recsys.db").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_a_file_that_disagrees_with_the_manifest_is_refused(pack, tmp_path):
    archive, digest = tampered(pack, tmp_path, {"ATTRIBUTION.md": b"changed"})
    with pytest.raises(StarterError, match="manifest"):
        install_pack(str(archive), tmp_path / "data", sha256=digest)


def test_your_ratings_are_never_replaced_even_with_force(pack, tmp_path):
    data = tmp_path / "data"
    init_db(data / "recsys.db")
    conn = connect(data / "recsys.db")
    upsert_items(conn, [row("book", "goodbooks", 9, "Mine")])
    set_rating(conn, "book:goodbooks:9", 7)
    conn.close()
    with pytest.raises(StarterError, match="your own ratings"):
        install_pack(str(pack.archive), data, force=True)


def test_an_existing_catalog_needs_force(pack, tmp_path):
    data = tmp_path / "data"
    init_db(data / "recsys.db")
    conn = connect(data / "recsys.db")
    upsert_items(conn, [row("book", "goodbooks", 9, "Old")])
    conn.close()
    with pytest.raises(StarterError, match="--force"):
        install_pack(str(pack.archive), data)
    assert install_pack(str(pack.archive), data, force=True).replaced_catalog


def test_a_url_is_downloaded_and_checked_against_its_published_checksum(pack, tmp_path):
    body = pack.archive.read_bytes()

    def handler(request):
        if request.url.path.endswith(".sha256"):
            return httpx.Response(200, text=f"{pack.sha256}  loom-starter-test.zip\n")
        return httpx.Response(200, content=body, headers={"Content-Length": str(len(body))})

    url = "https://example.invalid/releases/loom-starter-test.zip"
    result = install_pack(url, tmp_path / "data", transport=httpx.MockTransport(handler))
    assert result.manifest["pack"] == "test"
    assert (tmp_path / "data" / "recsys.db").is_file()


def test_an_unpublished_pack_says_so(tmp_path):
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    with pytest.raises(StarterError, match="no starter pack is published"):
        install_pack("https://example.invalid/x.zip", tmp_path / "data", transport=transport)


def test_the_archive_holds_only_known_files(pack):
    with zipfile.ZipFile(pack.archive) as zf:
        assert set(zf.namelist()) <= set(PACK_FILES)


def test_bootstrap_installs_the_pack_from_the_command_line(pack, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bootstrap", Path(__file__).resolve().parent.parent / "scripts" / "bootstrap.py"
    )
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    assert bootstrap.main(["--starter", str(pack.archive)]) == 0
    out = capsys.readouterr().out
    assert "installed pack test" in out and "CC BY-SA 4.0" in out
    assert bootstrap.main(["--starter", str(pack.archive)]) == 1  # a catalog exists now


# ---- review: failing closed, atomicity, a strict manifest -------------------


def with_your_rating(data: Path) -> Path:
    init_db(data / "recsys.db")
    conn = connect(data / "recsys.db")
    upsert_items(conn, [row("book", "goodbooks", 9, "Mine")])
    set_rating(conn, "book:goodbooks:9", 7)
    conn.close()
    return data / "recsys.db"


def test_ratings_under_a_path_with_a_hash_are_still_seen(pack, tmp_path):
    """A hand-built file: URI read "#" as a fragment, opened another file, and
    reported no ratings -- and the install replaced them."""
    db = with_your_rating(tmp_path / "C#user" / "Lo%20om")
    with pytest.raises(StarterError, match="your own ratings"):
        install_pack(str(pack.archive), db.parent, force=True)
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == 1
    conn.close()


def test_a_database_that_cannot_be_read_is_never_replaced(pack, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "recsys.db").write_bytes(b"not a database at all, but maybe yours")
    with pytest.raises(StarterError, match="could not be read"):
        install_pack(str(pack.archive), data, force=True)
    assert (data / "recsys.db").read_bytes().startswith(b"not a database")


def test_a_rating_made_during_the_download_stops_the_install(pack, tmp_path, monkeypatch):
    import recsys.starter as starter

    data = tmp_path / "data"
    init_db(data / "recsys.db")
    real_fetch = starter._fetch

    def fetch_while_you_rate(location, dest, transport=None):
        conn = connect(data / "recsys.db")
        upsert_items(conn, [row("book", "goodbooks", 9, "Rated meanwhile")])
        set_rating(conn, "book:goodbooks:9", 9)
        conn.close()
        return real_fetch(location, dest, transport)

    monkeypatch.setattr(starter, "_fetch", fetch_while_you_rate)
    with pytest.raises(StarterError, match="your own ratings"):
        install_pack(str(pack.archive), data, force=True)
    conn = connect(data / "recsys.db")
    assert conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == 1
    conn.close()


def old_build(data: Path) -> None:
    init_db(data / "recsys.db")
    conn = connect(data / "recsys.db")
    upsert_items(conn, [row("anime", "anilist", 1, "Old catalog")])
    conn.close()
    (data / "features").mkdir(parents=True, exist_ok=True)
    (data / "features" / "embeddings.parquet").write_bytes(b"old 84k matrix")
    (data / "features" / "collaborative.parquet").write_bytes(b"old cf")
    (data / "build-state.json").write_text('{"steps": {}}', encoding="utf-8")


def test_a_failed_swap_puts_everything_back(pack, tmp_path, monkeypatch):
    import recsys.starter as starter

    data = tmp_path / "data"
    old_build(data)
    real_replace = starter.os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if str(src).endswith("embeddings.parquet") and ".starter-unpack" in str(src):
            raise PermissionError("locked by another process")
        return real_replace(src, dst)

    monkeypatch.setattr(starter.os, "replace", flaky)
    with pytest.raises(StarterError, match="nothing was changed"):
        install_pack(str(pack.archive), data, force=True)
    assert (data / "features" / "embeddings.parquet").read_bytes() == b"old 84k matrix"
    assert (data / "build-state.json").is_file()
    conn = connect(data / "recsys.db")
    assert conn.execute("SELECT title FROM items").fetchone()[0] == "Old catalog"
    conn.close()


def test_force_leaves_nothing_of_the_old_build_behind(pack, tmp_path):
    """An old matrix the pack lacks, or the full build's record of steps done,
    would describe a catalog that is no longer there."""
    data = tmp_path / "data"
    old_build(data)
    (data / "features" / "embeddings.parquet").unlink()  # pack has one; test the other
    (data / "recsys.db-wal").write_bytes(b"stale")
    result = install_pack(str(pack.archive), data, force=True)
    assert not (data / "build-state.json").exists()
    assert not (data / "recsys.db-wal").exists() or (data / "recsys.db-wal").read_bytes() != b"stale"
    assert (data / "features" / "collaborative.parquet").read_bytes() != b"old cf"
    assert result.backup is not None and (result.backup / "build-state.json").is_file()


def test_a_manifest_that_leaves_a_file_out_is_refused(pack, tmp_path):
    with zipfile.ZipFile(pack.archive) as zf:
        manifest = json.loads(zf.read("MANIFEST.json"))
    del manifest["files"]["ATTRIBUTION.md"]
    archive, digest = tampered(pack, tmp_path, {"MANIFEST.json": json.dumps(manifest).encode()})
    with pytest.raises(StarterError, match="account for exactly"):
        install_pack(str(archive), tmp_path / "data", sha256=digest)


def test_a_member_of_the_wrong_size_is_refused_before_extracting(pack, tmp_path):
    with zipfile.ZipFile(pack.archive) as zf:
        manifest = json.loads(zf.read("MANIFEST.json"))
    manifest["files"]["ATTRIBUTION.md"]["bytes"] += 1
    archive, digest = tampered(pack, tmp_path, {"MANIFEST.json": json.dumps(manifest).encode()})
    with pytest.raises(StarterError, match="size"):
        install_pack(str(archive), tmp_path / "data", sha256=digest)


def test_the_ratings_used_for_the_model_leave_no_trace_in_the_pack(pack, tmp_path):
    """Deleted and VACUUMed: not a row, not a free page, not a byte."""
    raw = (unpacked(pack, tmp_path) / "recsys.db").read_bytes()
    assert b"goodbooks:5" not in raw
    assert b"mal:someone" not in raw
    assert b"local:you" not in raw


def test_the_attribution_says_what_was_changed_and_where_covers_come_from(pack, tmp_path):
    text = (unpacked(pack, tmp_path) / "ATTRIBUTION.md").read_text(encoding="utf-8")
    assert "Changes made" in text
    assert "images.gr-assets.com" in text and "covers.openlibrary.org" in text



def test_a_vector_ships_only_if_it_was_made_from_the_packs_own_document(source, tmp_path):
    """A vector without a fingerprint, or with another document's, could have
    been made from text the pack may not carry; it stays behind."""
    from recsys.models.text import doc_fingerprint

    path = source / "features" / "embeddings.parquet"
    table = pq.read_table(path)
    hashes = table.column("doc_hash").to_pylist()
    uids = table.column("item_uid").to_pylist()
    changed = [doc_fingerprint("some other text", MODEL) if u == "book:goodbooks:2"
               else (None if u == "book:goodbooks:3" else h)
               for u, h in zip(uids, hashes, strict=True)]
    pq.write_table(table.drop(["doc_hash"]).append_column("doc_hash", pa.array(changed)), path)

    result = build_pack(
        source / "recsys.db", embeddings=path, out_dir=tmp_path / "out2", version="t2",
        rebuild_derived=rebuild_derived, build_collaborative=cf, model=MODEL,
    )
    with zipfile.ZipFile(result.archive) as zf:
        zf.extract("features/embeddings.parquet", tmp_path / "x")
    shipped = set(pq.read_table(tmp_path / "x" / "features" / "embeddings.parquet")
                  .column("item_uid").to_pylist())
    assert "book:goodbooks:2" not in shipped and "book:goodbooks:3" not in shipped
    assert result.missing_vectors == 2
