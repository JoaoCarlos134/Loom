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
    # `anime_jikan_text` asks for one title per request, so this is a ceiling on
    # requests, not on pages. 2,935 anime works carry no synopsis at all; at
    # Jikan's 60/min that is about 50 minutes, and the cap exists so a run can
    # be bounded rather than because the work should stop short of finishing.
    jikan_text_target_count: int = 20000
    jikan_text_max_retries: int = 2

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
    # Sweep filters. An anime nobody has on a list has no audience signal, no
    # reliable score, and cannot be meaningfully recommended -- it is catalogue
    # weight without catalogue value. 0 disables the floor.
    anilist_min_popularity: int = 200
    # Earliest start-year the partitioned sweep walks back to. Commercial anime
    # begins in the 1910s but anything before this is a handful of shorts.
    anilist_first_year: int = 1950

    # AniList tags are community-ranked 0-100; the long tail is weakly agreed.
    anilist_min_tag_rank: int = 60

    # TMDB (movies / TV). Credentials are secrets and live only in .env.
    # TMDB_READ_TOKEN is preferred: it goes in an Authorization header rather
    # than a query string, so it never reaches a URL, a log or a cache file.
    tmdb_read_token: str = ""
    tmdb_api_key: str = ""
    tmdb_base_url: str = "https://api.themoviedb.org/3"
    tmdb_image_base: str = "https://image.tmdb.org/t/p/w500"
    # 0 -> TMDB's own ceiling: /discover stops at 500 pages of 20.
    tmdb_target_count: int = 10000
    # Minimum votes for a title to be worth carrying. Measured on a first
    # unfiltered pass: films below 50 votes were 22% of the catalog, shows
    # below 25 were 53% -- television has a far longer obscure tail, so it
    # takes its own floor rather than the film one.
    tmdb_min_votes: int = 50
    tmdb_tv_min_votes: int = 25
    tmdb_rate_per_sec: float = 20.0
    tmdb_rate_per_min: float = 0.0  # 0 disables the per-minute window
    tmdb_max_retries: int = 5

    # goodbooks-10k (books). Three small CSVs served from GitHub, no credential.
    goodbooks_base_url: str = (
        "https://raw.githubusercontent.com/zygmuntz/goodbooks-10k/master"
    )
    goodbooks_path: str = ""  # a local directory overrides the download
    goodbooks_target_count: int = 0  # 0 = the whole file
    # A shelf used by fewer readers than this is one person's filing habit.
    goodbooks_min_tag_count: int = 50
    goodbooks_max_tags: int = 20
    # goodbooks-10k user ratings (~6M), same base URL / GOODBOOKS_PATH as the
    # catalog. Rows read, not rows written. 0 = the whole file.
    goodbooks_ratings_target_count: int = 0

    # Open Library (book descriptions). Keyless. Driven by the ISBNs already in
    # the catalog, so book_goodbooks runs first.
    openlibrary_url: str = "https://openlibrary.org"
    openlibrary_target_count: int = 0  # 0 = every ISBN in the catalog
    openlibrary_max_subjects: int = 25
    # Open Library documents 1 req/s for anonymous clients and 3 req/s for ones
    # that identify themselves with a contact User-Agent -- which this ingester
    # sends, so 3 is the ceiling. It was 5.0 here, over the limit, under a
    # comment asserting the opposite. They are a nonprofit on donated
    # infrastructure; the documented number is the number.
    openlibrary_rate_per_sec: float = 3.0
    openlibrary_rate_per_min: float = 0.0
    openlibrary_max_retries: int = 6

    # MusicBrainz + Deezer (music). Both keyless. The domain is driven by the
    # composers credited in the film/TV catalog, so movie_tmdb / tv_tmdb run
    # first. See music_musicbrainz.py for why it is not a general music dump.
    musicbrainz_url: str = "https://musicbrainz.org/ws/2"
    # MusicBrainz throttles clients whose User-Agent names no way to contact
    # them. This goes into that header; point it at your own repo or email.
    musicbrainz_contact: str = "https://github.com/JoaoCarlos134/Loom"
    deezer_url: str = "https://api.deezer.com"
    music_use_deezer: bool = True  # Deezer supplies the audience count and picture
    # Deezer allows 50 requests per 5 seconds per IP. 8/s keeps a margin.
    deezer_rate_per_sec: float = 8.0
    music_target_count: int = 0  # 0 = every credited composer
    # MusicBrainz scores each candidate 0-100; below this a name match is a guess.
    musicbrainz_min_score: int = 90
    # MusicBrainz asks for at most one request a second. Do not raise this.
    musicbrainz_rate_per_sec: float = 1.0
    # 1/s is their stated ceiling and they throttle at it, so the sustained rate
    # is held below it. Measured: at a flat 1/s, roughly one lookup in six came
    # back 503.
    musicbrainz_rate_per_min: float = 45.0
    musicbrainz_max_retries: int = 8

    # Wikidata's MyAnimeList <-> TMDB cross-references (CC0), one SPARQL query.
    # They are what lets the linker see that an anime and its own TMDB TV or
    # film entry are one work. A local JSON file (a saved query result)
    # overrides the request.
    wikidata_sparql_url: str = "https://query.wikidata.org/sparql"
    wikidata_idmap_path: str = ""
    wikidata_max_retries: int = 4

    # MyAnimeList user ratings, from the same Kaggle archive as the anime
    # table. Rows read, not rows written: ~40% carry no score and are skipped.
    mal_ratings_filename: str = "animelists_cleaned.csv"
    mal_ratings_path: str = ""
    mal_ratings_target_count: int = 0

    # --- read view ---
    # A group -- the works one story spans, joined by adaptation, franchise and
    # series edges -- larger than this is not treated as one story: its members
    # fall back to their direct edges. Transitive closure is what makes a third
    # season count as the first's franchise, and it is also how one crossover
    # special chains two franchises together. The biggest single franchises on
    # AniList run to roughly 100-200 entries (Gundam, Pokemon, Detective Conan),
    # so 250 sits above them. PROVISIONAL: not yet set from the full build's
    # distribution; bootstrap.py prints the largest groups so it can be.
    max_group_size: int = 250
    # goodbooks-10k files each volume as a book: Death Note is eight, Discworld
    # thirty-nine. Folded, a series is one card (its most read volume) and the
    # rest are a click away; measured on the real books.csv that is 852 series
    # holding 3,640 books, so 2,788 leave the shelf. False lists every volume
    # again; the series still counts as one story for recommendations.
    fold_book_series: bool = True

    # --- models ---
    # A small ONNX sentence embedder: 384 dimensions, ~67 MB, CPU-only, no
    # torch. Fetched once into DATA_DIR/models -- fastembed otherwise caches to
    # a temporary directory that Windows clears, silently re-downloading it.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_batch_size: int = 64
    # Measured on 20 cores over real catalog documents (median 512 characters):
    #   default                  194 docs/min   -> 7.2 hours for the catalog
    #   threads only             204 docs/min   -> no real change
    #   threads + parallel       557 docs/min   -> 2.5 hours
    # Threads alone does nothing because onnxruntime does not parallelise one
    # short sequence well; the gain comes from embedding batches in parallel
    # processes. 0 means "every core" for both, which is fastembed's convention.
    embedding_threads: int = 0
    embedding_parallel: int = 0

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
    def embeddings_path(self) -> Path:
        """The item embedding matrix. Derived, rebuildable, outside the repo."""
        return self.data_dir / "features" / "embeddings.parquet"

    @property
    def collaborative_path(self) -> Path:
        """The item-item neighbour table. Derived from `ratings`, same rules."""
        return self.data_dir / "features" / "collaborative.parquet"

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
