"""The starter pack: a working catalog, built only from data that may be shared.

A full build is hours of requests and needs a TMDB key and a Kaggle account.
The starter pack is what a stranger can have in a minute instead: the catalog,
the embedding matrix and the collaborative model, built from the sources whose
terms allow redistribution, and nothing else.

**What may ship is decided per source, here and nowhere else.** ``SHIPPABLE``
names each (domain, source) pair the pack may contain and the terms that allow
it. A pair not named is left out, whatever the build holds:

* goodbooks-10k -- CC BY-SA 4.0. Shippable with credit, ShareAlike on the data.
* Open Library -- its catalog is public domain / CC0. Descriptions are user
  contributions under the same dedication, though some reproduce publisher
  copy; Open Library publishes them in its own public dumps on those terms.
* TMDB, AniList, the MAL Kaggle dump, Jikan -- excluded. TMDB forbids
  redistribution and storage beyond six months; AniList forbids hoarding and
  competing list services; the MAL dump is scraped from MyAnimeList, whatever
  licence its uploader states.
* MusicBrainz -- left out although its core data is CC0: this domain is the
  composers credited in TMDB's films, so without TMDB it has no reason to
  exist, and its tags are CC BY-NC-SA, not CC0.

**Verified before anything is replaced.** The archive's SHA-256 is checked
against the value pinned in ``config.starter_sha256`` (or, failing that, the
``.sha256`` published beside it), then every file against ``MANIFEST.json``.
An archive member that is not one of the known pack files -- a path with
``..``, an absolute path, anything unexpected -- fails the install.

**It never overwrites your ratings.** A data directory whose database holds a
rating of yours is refused; the pack is for a fresh install.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recsys.log import get_logger

log = get_logger(__name__)

#: (domain, source) -> the terms that allow it to be redistributed.
SHIPPABLE: dict[tuple[str, str], str] = {
    ("book", "goodbooks"): "goodbooks-10k by Zygmunt Zajac, CC BY-SA 4.0",
    ("book", "openlibrary"): "Open Library (openlibrary.org), public domain / CC0",
}

#: User namespaces whose ratings may train the pack's collaborative model.
SHIPPABLE_RATINGS: dict[str, str] = {
    "goodbooks": "goodbooks-10k by Zygmunt Zajac, CC BY-SA 4.0",
}

DB_NAME = "recsys.db"
EMBEDDINGS = "features/embeddings.parquet"
COLLABORATIVE = "features/collaborative.parquet"
MANIFEST = "MANIFEST.json"
ATTRIBUTION = "ATTRIBUTION.md"

#: The only names an archive may contain. Anything else fails the install.
PACK_FILES = (DB_NAME, EMBEDDINGS, COLLABORATIVE, MANIFEST, ATTRIBUTION)


#: Every column of `items` a pack carries -- the catalog's own, raw_json included.
_ITEM_COLUMNS = (
    "item_uid", "domain", "source", "source_id", "title", "alt_titles", "year", "creators",
    "tags", "description", "external_ids", "popularity", "mean_rating", "n_ratings",
    "image_url", "fetched_at", "raw_json",
)


class StarterError(RuntimeError):
    """The pack could not be built or installed, and nothing was changed."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# building a pack (the maintainer's side)
# --------------------------------------------------------------------------


@dataclass
class PackResult:
    archive: Path
    sha256: str
    counts: dict[str, int] = field(default_factory=dict)
    missing_vectors: int = 0


def _attribution(counts: dict[str, int]) -> str:
    lines = [
        "# Loom starter pack -- attribution",
        "",
        "This archive holds data from the sources below, under their own terms.",
        "Loom's code is MIT-licensed; that licence does not cover this data.",
        "",
    ]
    for (domain, source), terms in SHIPPABLE.items():
        lines.append(f"- **{domain}/{source}** ({counts.get(f'{domain}:{source}', 0):,} rows): "
                     f"{terms}.")
    for namespace, terms in SHIPPABLE_RATINGS.items():
        lines.append(f"- **ratings/{namespace}** (used to train the collaborative model, "
                     f"not included as rows): {terms}.")
    lines += [
        "",
        "goodbooks-10k: https://github.com/zygmuntz/goodbooks-10k -- the adapted data in",
        "this archive (catalog rows, derived vectors and neighbours) is shared under the",
        "same licence, CC BY-SA 4.0: https://creativecommons.org/licenses/by-sa/4.0/",
        "",
        "Open Library: https://openlibrary.org/developers/licensing",
        "",
        "Changes made (as CC BY-SA asks): the goodbooks-10k rows are mapped into",
        "Loom's catalog schema (ratings rescaled from 1-5 to 1-10, shelves filtered,",
        "Goodreads' 'nophoto' placeholder dropped), linked to Open Library records",
        "by ISBN, grouped into series from their titles, and turned into text-",
        "embedding vectors and an item-item neighbour table. Nothing else is added.",
        "",
        "Images are not included, only links: book covers are served by",
        "covers.openlibrary.org and by Goodreads' image server",
        "(images.gr-assets.com), under their own terms.",
        "",
    ]
    return "\n".join(lines)


def build_pack(
    source_db: Path,
    *,
    embeddings: Path | None,
    out_dir: Path,
    version: str,
    rebuild_derived,
    build_collaborative=None,
    model: str = "",
) -> PackResult:
    """Copy the shippable rows of ``source_db`` into a new pack. Reads the source only.

    ``rebuild_derived(conn)`` rebuilds links, popularity, relations and works on
    the pack's own database -- they describe the build they were made from, and
    the pack is a different catalog. ``build_collaborative(conn, out_path)``
    trains the neighbour table on the shippable ratings; the ratings themselves
    are then dropped from the pack, which keeps it small (a 72 MB download the
    user can fetch in a minute if they want them).
    """
    from recsys.db import connect, init_db

    out_dir = Path(out_dir)
    stage = out_dir / f"loom-starter-{version}"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "features").mkdir(parents=True)
    db_path = stage / DB_NAME
    init_db(db_path)

    # The source is attached read-only. SQLite reads "file:...?mode=ro" as a
    # URI in ATTACH only on a connection opened with URIs enabled, hence this
    # one rather than db.connect.
    copier = sqlite3.connect(db_path.resolve().as_uri(), uri=True)
    try:
        copier.execute("ATTACH DATABASE ? AS src",
                       (Path(source_db).resolve().as_uri() + "?mode=ro",))
        pairs = " OR ".join("(domain = ? AND source = ?)" for _ in SHIPPABLE)
        spaces = " OR ".join("user_uid LIKE ?" for _ in SHIPPABLE_RATINGS)
        # Columns named, not `*`: a source built by another Loom version may
        # order or extend them differently.
        cols = ", ".join(_ITEM_COLUMNS)
        with copier:
            copier.execute(
                f"INSERT INTO items ({cols}) SELECT {cols} FROM src.items WHERE {pairs}",
                [v for pair in SHIPPABLE for v in pair],
            )
            copier.execute(
                "INSERT INTO ratings (user_uid, item_uid, rating, rated_at) "
                f"SELECT user_uid, item_uid, rating, rated_at FROM src.ratings WHERE {spaces}",
                [f"{ns}:%" for ns in SHIPPABLE_RATINGS],
            )
        copier.execute("DETACH DATABASE src")
    finally:
        copier.close()

    conn = connect(db_path)
    counts = {
        f"{r['domain']}:{r['source']}": r["n"]
        for r in conn.execute("SELECT domain, source, COUNT(*) AS n FROM items GROUP BY 1, 2")
    }
    counts["ratings"] = int(conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0])
    rebuild_derived(conn)
    counts["works"] = int(conn.execute("SELECT COUNT(*) FROM works").fetchone()[0])

    if build_collaborative is not None and counts["ratings"]:
        build_collaborative(conn, stage / COLLABORATIVE)
    with conn:
        conn.execute("DELETE FROM ratings")
    conn.execute("VACUUM")

    missing = 0
    if embeddings is not None:
        missing = _filter_matrix(conn, Path(embeddings), stage / EMBEDDINGS, model)
    conn.close()
    for sidecar in ("-wal", "-shm"):
        Path(f"{db_path}{sidecar}").unlink(missing_ok=True)

    (stage / ATTRIBUTION).write_text(_attribution(counts), encoding="utf-8")
    files = {
        name: {"sha256": sha256_of(stage / name), "bytes": (stage / name).stat().st_size}
        for name in PACK_FILES
        if name != MANIFEST and (stage / name).is_file()
    }
    from recsys import __version__
    from recsys.db import SCHEMA_VERSION

    manifest = {
        "pack": version,
        "loom": __version__,
        "schema_version": SCHEMA_VERSION,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sources": {f"{d}:{s}": t for (d, s), t in SHIPPABLE.items()},
        "counts": counts,
        "files": files,
    }
    (stage / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    archive = out_dir / f"loom-starter-{version}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in PACK_FILES:
            if (stage / name).is_file():
                zf.write(stage / name, arcname=name)
    digest = sha256_of(archive)
    Path(f"{archive}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    shutil.rmtree(stage)
    return PackResult(archive=archive, sha256=digest, counts=counts, missing_vectors=missing)


def _filter_matrix(conn: sqlite3.Connection, source: Path, out: Path, model: str) -> int:
    """Ship the source matrix's vectors for the pack's works. Returns how many lack one.

    A vector ships only when its fingerprint matches the document the pack
    itself would embed for that work: that is what shows it was made from the
    pack's own -- shareable -- rows and from nothing else in the build it came
    from. A vector with no fingerprint (from before fingerprints) or another
    document's does not ship; `embed.py --adopt` or a rebuild settles it.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    from recsys.models.text import doc_fingerprint, work_documents

    expected = {uid: doc_fingerprint(doc, model) for uid, _, doc in work_documents(conn)
                if doc.strip()}
    table = pq.read_table(source)
    if "doc_hash" in table.column_names:
        keep = [expected.get(u) == h and h is not None for u, h in zip(
            table.column("item_uid").to_pylist(), table.column("doc_hash").to_pylist(),
            strict=True)]
    else:
        keep = [False] * table.num_rows
    kept = table.filter(pa.array(keep, type=pa.bool_()))
    pq.write_table(kept, out, compression="zstd")
    return len(set(expected) - set(kept.column("item_uid").to_pylist()))


# --------------------------------------------------------------------------
# installing a pack (everyone else's side)
# --------------------------------------------------------------------------


def _client(transport: Any = None):
    import httpx

    from recsys.net import enable_os_truststore

    enable_os_truststore()
    return httpx.Client(
        follow_redirects=True, timeout=httpx.Timeout(60.0, connect=15.0), transport=transport,
        headers={"User-Agent": "Loom/0.1 ( https://github.com/JoaoCarlos134/Loom )"},
    )


def _fetch(location: str, dest: Path, transport: Any = None) -> Path:
    """A local path is used as it is; a URL is downloaded to ``dest``."""
    if "://" not in location:
        path = Path(location)
        if not path.is_file():
            raise StarterError(f"no starter pack at {path}")
        return path
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    with _client(transport) as client, client.stream("GET", location) as resp:
        if resp.status_code == 404:
            raise StarterError(
                f"{location} does not exist (HTTP 404). No starter pack is published "
                "there yet; see the README's 'Get the data' for building the catalog "
                "yourself."
            )
        if resp.status_code != 200:
            raise StarterError(f"{location} answered HTTP {resp.status_code}")
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with partial.open("wb") as fh:
            for chunk in resp.iter_bytes(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  downloading {done / 1e6:.0f} / {total / 1e6:.0f} MB",
                          end="", flush=True)
    if total:
        print()
    partial.replace(dest)
    return dest


def _expected_digest(location: str, pinned: str, transport: Any = None) -> str:
    """The pinned checksum, else the ``.sha256`` published beside the archive.

    The pinned value is the stronger check: it comes with the code, so it
    catches an archive replaced on the server along with its sidecar. The
    sidecar alone catches a corrupt or truncated download.
    """
    if pinned:
        return pinned.strip().lower()
    if "://" not in location:
        sidecar = Path(f"{location}.sha256")
        if not sidecar.is_file():
            raise StarterError(f"no checksum: pass --sha256 or put {sidecar.name} beside it")
        return sidecar.read_text(encoding="utf-8").split()[0].lower()
    with _client(transport) as client:
        resp = client.get(f"{location}.sha256")
    if resp.status_code != 200:
        raise StarterError(
            f"no checksum published at {location}.sha256 (HTTP {resp.status_code}); "
            "no starter pack is published there yet"
        )
    return str(resp.text).split()[0].lower()


def _ro_uri(db_path: Path) -> str:
    """A read-only URI for any path. ``as_uri`` percent-encodes ``#`` and ``%``,
    which a hand-built ``file:`` URI would read as a fragment or an escape and
    so open a different file than the one meant."""
    return Path(db_path).resolve().as_uri() + "?mode=ro"


def _has_your_ratings(db_path: Path) -> bool:
    """Whether ``db_path`` holds a rating of yours. Fails *closed*.

    Anything but a clear "no" -- an unreadable file, a locked one, a database
    with no ratings table yet only because it is not a Loom one -- counts as
    "yes", except that last case, which is checked for explicitly. Losing your
    ratings to a guard that could not read them is the one outcome this must
    never allow.
    """
    from recsys.schemas import LOCAL_USER_UID

    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(_ro_uri(db_path), uri=True)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "ratings" not in tables:
                return False
            row = conn.execute(
                "SELECT 1 FROM ratings WHERE user_uid = ? LIMIT 1", (LOCAL_USER_UID,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return True
    return row is not None


def _has_catalog(db_path: Path) -> bool:
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(_ro_uri(db_path), uri=True)
        try:
            return conn.execute("SELECT 1 FROM items LIMIT 1").fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return True  # unreadable: treat as a catalog, so only --force replaces it


def _refuse_if_yours(db_path: Path) -> None:
    if _has_your_ratings(db_path):
        raise StarterError(
            f"{db_path} holds your own ratings (or could not be read to check); the "
            "starter pack will not replace it. Move it aside, or point DATA_DIR "
            "elsewhere, and run this again."
        )


#: What an install replaces in ``data_dir`` -- the pack's own files, plus what
#: describes the catalog they replace: the database's sidecars and the full
#: build's record of what it did, which would otherwise claim the old build's
#: steps are done.
_REPLACED = (DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm", "features", "build-state.json",
             ATTRIBUTION, MANIFEST)

#: Members above this are refused before anything is extracted. The pack is
#: tens of megabytes; this is a ceiling on what a zip bomb could unpack.
MAX_MEMBER_BYTES = 4 * 1024**3


def _unpack(archive: Path, stage: Path) -> dict[str, Any]:
    """Extract and verify. Every member must be a known pack file, named in the
    manifest with its size and checksum; sizes are checked before extracting."""
    with zipfile.ZipFile(archive) as zf:
        infos = {i.filename: i for i in zf.infolist()}
        unexpected = [n for n in infos if n not in PACK_FILES]
        if unexpected or MANIFEST not in infos or DB_NAME not in infos:
            raise StarterError(f"not a Loom starter pack (unexpected: {unexpected[:3]})")
        manifest: dict[str, Any] = json.loads(zf.read(MANIFEST).decode("utf-8"))
        files = manifest.get("files", {})
        if set(files) != set(infos) - {MANIFEST}:
            raise StarterError("the pack's manifest does not account for exactly its files")
        for name, meta in files.items():
            size = infos[name].file_size
            if size > MAX_MEMBER_BYTES or size != meta.get("bytes"):
                raise StarterError(f"{name} is not the size the pack's manifest states")
        for name in infos:
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    for name, meta in files.items():
        if sha256_of(stage / name) != meta.get("sha256"):
            raise StarterError(f"{name} does not match the pack's manifest")
    return manifest


def _swap_in(stage: Path, data_dir: Path, names: list[str]) -> Path:
    """Move the old set aside, the new one in; put the old one back on any error.

    Returns the backup directory, which holds whatever the install replaced.
    """
    backup = data_dir / f".starter-replaced-{datetime.now(UTC):%Y%m%dT%H%M%S}"
    moved: list[str] = []
    placed: list[str] = []
    try:
        for name in _REPLACED:
            if (data_dir / name).exists():
                (backup / name).parent.mkdir(parents=True, exist_ok=True)
                os.replace(data_dir / name, backup / name)
                moved.append(name)
        for name in names:
            dest = data_dir / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage / name, dest)
            placed.append(name)
    except OSError as exc:
        for name in placed:
            (data_dir / name).unlink(missing_ok=True)
        for name in moved:
            os.replace(backup / name, data_dir / name)
        raise StarterError(
            f"could not put the pack in place ({exc}); nothing was changed. Close Loom if "
            "it is running and try again."
        ) from exc
    return backup


@dataclass
class InstallResult:
    manifest: dict[str, Any]
    replaced_catalog: bool = False
    #: Where the replaced catalog went (with --force); delete it once unneeded.
    backup: Path | None = None


def install_pack(
    location: str,
    data_dir: Path,
    *,
    sha256: str = "",
    force: bool = False,
    transport: Any = None,
) -> InstallResult:
    """Download (if needed), verify and unpack a starter pack into ``data_dir``.

    Refuses, changing nothing, when: the checksum does not match; the archive
    holds anything but the known pack files; a file does not match the
    manifest; the data directory already has a catalog (unless ``force``); or
    it holds a rating of yours (always -- move that database aside first).
    """
    data_dir = Path(data_dir)
    db_path = data_dir / DB_NAME
    _refuse_if_yours(db_path)
    replaced = _has_catalog(db_path)
    if replaced and not force:
        raise StarterError(
            f"{db_path} already holds a catalog; pass --force to replace it with the pack"
        )

    expected = _expected_digest(location, sha256, transport)
    archive = _fetch(location, data_dir / "downloads" / location.rsplit("/", 1)[-1], transport)
    actual = sha256_of(archive)
    if actual != expected:
        raise StarterError(f"checksum mismatch for {archive.name}: expected {expected}, "
                           f"got {actual}; the download is corrupt or not the published pack")

    stage = data_dir / ".starter-unpack"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    try:
        manifest = _unpack(archive, stage)
        # Checked again: the download can take minutes, and Loom may have been
        # open and taken a rating meanwhile.
        _refuse_if_yours(db_path)
        backup = _swap_in(stage, data_dir, [n for n in PACK_FILES if (stage / n).is_file()])
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    if not replaced:
        shutil.rmtree(backup, ignore_errors=True)  # it held nothing of a catalog
    log.info("starter pack %s installed into %s", manifest.get("pack"), data_dir)
    return InstallResult(manifest=manifest, replaced_catalog=replaced,
                         backup=backup if replaced else None)
