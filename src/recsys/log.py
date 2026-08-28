"""Stdlib logging setup. No third-party logging dependency."""

from __future__ import annotations

import logging
import logging.handlers
import sys

_CONFIGURED = False

_FMT = "%(asctime)s %(levelname)-7s %(name)-24s %(message)s"
_DATEFMT = "%H:%M:%S"


def setup_logging(level: str | None = None) -> None:
    """Idempotent root-logger configuration: console + rotating file."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    from recsys.config import get_settings

    s = get_settings()
    s.ensure_dirs()
    lvl = getattr(logging, (level or s.log_level).upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(lvl)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter(_FMT, _DATEFMT))
    root.addHandler(console)

    fileh = logging.handlers.RotatingFileHandler(
        s.log_dir / "recsys.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    fileh.setFormatter(logging.Formatter(_FMT.replace("%(asctime)s", "%(asctime)s"), None))
    root.addHandler(fileh)

    # httpx logs every request at INFO; too chatty during a 80-page ingest.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
