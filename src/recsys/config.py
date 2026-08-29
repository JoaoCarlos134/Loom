"""Application settings.

Single source of truth for every tunable. Read once from ``.env`` + environment,
cached for the process lifetime. Nothing else in the codebase may read os.environ.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root: config.py -> recsys -> src -> <root>
REPO_ROOT = Path(__file__).resolve().parents[2]


def _default_data_dir() -> Path:
    """Per-user writable data dir, deliberately outside the repo.

    The repo may live in a OneDrive/Dropbox-synced folder; those clients take
    file locks and mangle SQLite WAL sidecars, producing spurious
    "database is locked" errors. %LOCALAPPDATA% is never synced.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "Loom"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "loom"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- general ---
    region: str = "BR"
    log_level: str = "INFO"
    data_dir: Path = Field(default_factory=_default_data_dir)

    # --- ingest ---
    ingesters: str = "anime_jikan"

    jikan_base_url: str = "https://api.jikan.moe/v4"
    jikan_target_count: int = 2000
    jikan_page_size: int = 25
    jikan_rate_per_sec: float = 3.0
    jikan_rate_per_min: float = 60.0
    jikan_max_retries: int = 5

    # MyAnimeList CSV dump (Kaggle). Path empty -> fetched via kagglehub and
    # cached under ~/.cache/kagglehub. target 0 -> ingest the whole file.
    mal_csv_dataset: str = "azathoth42/myanimelist"
    mal_csv_filename: str = "AnimeList.csv"
    mal_csv_path: str = ""
    mal_csv_target_count: int = 0

    # AniList GraphQL: the source that carries synopses and ranked tags.
    # Driven by the MAL ids already in the catalog, so it runs after
    # anime_mal_csv. target 0 -> every known id.
    anilist_url: str = "https://graphql.anilist.co"
    anilist_target_count: int = 0
    anilist_page_size: int = 50  # AniList caps a page at 50
    anilist_rate_per_sec: float = 1.0
    anilist_rate_per_min: float = 30.0  # observed x-ratelimit-limit
    anilist_max_retries: int = 5
    # AniList tags are community-ranked 0-100; the long tail is weakly agreed.
    anilist_min_tag_rank: int = 60

    # MyAnimeList user ratings, from the same Kaggle archive as the anime
    # table. Rows read, not rows written: ~40% carry no score and are skipped.
    mal_ratings_filename: str = "animelists_cleaned.csv"
    mal_ratings_path: str = ""
    mal_ratings_target_count: int = 0

    http_cache_ttl_days: int = 30

    # --- server ---
    host: str = "127.0.0.1"
    port: int = 0  # 0 -> pick a free ephemeral port

    @field_validator("region")
    @classmethod
    def _upper_region(cls, v: str) -> str:
        return v.strip().upper()

    @property
    def enabled_ingesters(self) -> list[str]:
        return [n.strip() for n in self.ingesters.split(",") if n.strip()]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "recsys.db"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.cache_dir, self.log_dir):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
