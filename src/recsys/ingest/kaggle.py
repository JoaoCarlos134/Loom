"""Locating a file inside a Kaggle dataset.

Shared by the two ingesters that read the ``azathoth42/myanimelist`` dump --
the anime table and the user-ratings table -- because they resolve the same
archive and only the filename differs.
"""

from __future__ import annotations

from pathlib import Path

from recsys.log import get_logger

log = get_logger(__name__)


def resolve_kaggle_file(
    *, dataset: str, filename: str, explicit_path: str, setting_name: str
) -> Path:
    """Return a local path to ``filename``, downloading the dataset if needed.

    ``explicit_path`` short-circuits the download entirely; ``setting_name`` is
    only there so the error names the environment variable the user actually
    set. kagglehub caches under ``~/.cache/kagglehub``, so the archive is
    fetched at most once per machine and later runs are offline.
    """
    if explicit_path:
        path = Path(explicit_path)
        if not path.is_file():
            raise FileNotFoundError(f"{setting_name} does not exist: {path}")
        return path

    # Imported lazily: only these ingesters need kagglehub, and only when they
    # have not been pointed at a local file.
    import kagglehub

    log.info("resolving %s via kagglehub (cached after the first run)", dataset)
    root = Path(kagglehub.dataset_download(dataset))
    path = root / filename
    if not path.is_file():
        available = sorted(p.name for p in root.glob("*.csv"))
        raise FileNotFoundError(f"{filename!r} not in {root}; available: {available}")
    return path
