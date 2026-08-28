"""On-disk HTTP response cache, keyed by URL.

Re-running an ingest must not re-hit the upstream API. Entries live under
``<DATA_DIR>/cache/<namespace>/`` as JSON envelopes, sharded two levels deep by
the key prefix so no single directory holds thousands of files (Windows
directory enumeration degrades badly there).

The cache also preserves the raw upstream payload, which is what makes a
mapping bug fixable offline: correct ``map_item`` and re-run.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from recsys.log import get_logger

log = get_logger(__name__)


class DiskCache:
    def __init__(self, root: Path, namespace: str, ttl_days: int = 30) -> None:
        self.dir = Path(root) / namespace
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_days * 86400 if ttl_days > 0 else 0  # 0 = never expire
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def path_for(self, url: str) -> Path:
        k = self.key(url)
        return self.dir / k[:2] / k[2:4] / f"{k}.json"

    def get(self, url: str) -> Any | None:
        path = self.path_for(url)
        if not path.is_file():
            self.misses += 1
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("discarding corrupt cache entry %s", path.name)
            path.unlink(missing_ok=True)
            self.misses += 1
            return None

        if self.ttl_seconds and time.time() - envelope.get("stored_at", 0) > self.ttl_seconds:
            self.misses += 1
            return None

        self.hits += 1
        return envelope.get("body")

    def set(self, url: str, body: Any) -> None:
        path = self.path_for(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        envelope = {"url": url, "stored_at": time.time(), "body": body}
        # Atomic write: a killed ingest must never leave a half-written entry
        # that later reads back as valid-looking JSON.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(envelope, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def clear(self) -> int:
        n = 0
        for p in self.dir.rglob("*.json"):
            p.unlink(missing_ok=True)
            n += 1
        return n
