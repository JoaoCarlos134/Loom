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
        "Open Library: https://openlibrary.org/developers/licensing -- covers are",
        "linked, not included, and are served by covers.openlibrary.org.",
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
        with copier:
            copier.execute(
                f"INSERT INTO items SELECT * FROM src.items WHERE {pairs}",
                [v for pair in SHIPPABLE for v in pair],
            )
            copier.execute(
                f"INSERT INTO ratings SELECT * FROM src.ratings WHERE {spaces}",
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
        missing = _filter_matrix(conn, Path(embeddings), stage / EMBEDDINGS)
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


def _filter_matrix(conn: sqlite3.Connection, source: Path, out: Path) -> int:
    """Keep the source matrix's vectors for the pack's works. Returns how many lack one.

    The vectors are carried, not recomputed: a book's document is its own rows'
    text and tags, all from shippable sources, so its vector is too.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    wanted = [r[0] for r in conn.execute("SELECT item_uid FROM works")]
    table = pq.read_table(source)
    kept = table.filter(pc.is_in(table.column("item_uid"), value_set=pa.array(wanted)))
    pq.write_table(kept, out, compression="zstd")
    return len(set(wanted) - set(kept.column("item_uid").to_pylist()))


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


def _has_your_ratings(db_path: Path) -> bool:
    from recsys.schemas import LOCAL_USER_UID

    if not db_path.is_file():
        return False
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT 1 FROM ratings WHERE user_uid = ? LIMIT 1", (LOCAL_USER_UID,)
        ).fetchone()
    except sqlite3.OperationalError:  # no ratings table: not a Loom database yet
        row = None
    finally:
        conn.close()
    return row is not None


def _has_catalog(db_path: Path) -> bool:
    if not db_path.is_file():
        return False
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT 1 FROM items LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


@dataclass
class InstallResult:
    manifest: dict[str, Any]
    replaced_catalog: bool = False


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
    if _has_your_ratings(db_path):
        raise StarterError(
            f"{db_path} holds your own ratings; the starter pack will not replace it. "
            "Move it aside (or point DATA_DIR elsewhere) and run this again."
        )
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
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            unexpected = [n for n in names if n not in PACK_FILES]
            if unexpected or MANIFEST not in names or DB_NAME not in names:
                raise StarterError(f"not a Loom starter pack (unexpected: {unexpected[:3]})")
            for name in names:
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        manifest = json.loads((stage / MANIFEST).read_text(encoding="utf-8"))
        for name, meta in manifest.get("files", {}).items():
            if name not in PACK_FILES or not (stage / name).is_file():
                raise StarterError(f"the manifest names {name!r}, which the archive lacks")
            if sha256_of(stage / name) != meta.get("sha256"):
                raise StarterError(f"{name} does not match the pack's manifest")

        for sidecar in ("", "-wal", "-shm"):
            Path(f"{db_path}{sidecar}").unlink(missing_ok=True)
        for name in names:
            dest = data_dir / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            (stage / name).replace(dest)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    log.info("starter pack %s installed into %s", manifest.get("pack"), data_dir)
    return InstallResult(manifest=manifest, replaced_catalog=replaced)
