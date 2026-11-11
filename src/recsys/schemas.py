"""Domain-agnostic data contracts.

``UnifiedItem`` is the single row shape every domain (anime / book / music /
movie / tv) collapses into. Adding a domain must never require changing this
file beyond widening the ``Domain`` literal.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Domain = Literal["anime", "manga", "book", "music", "movie", "tv"]

DOMAINS: tuple[str, ...] = ("anime", "manga", "book", "music", "movie", "tv")


def utcnow() -> datetime:
    return datetime.now(UTC)


def make_item_uid(domain: str, source: str, source_id: str) -> str:
    """The one place the item uid format is written down.

    ``UnifiedItem`` derives its ``item_uid`` through here, and so does anything
    that has to *point at* an item without holding one -- ratings, for example,
    which carry a source's own anime id and nothing else.
    """
    return f"{domain}:{source}:{source_id}"


def make_work_uid(domain: str, key: str, value: str) -> str:
    """Identify a *work* -- the thing several source rows describe.

    ``anime:mal:5114`` and ``anime:anilist:5114`` are two rows about one work.
    Both map to ``work:anime:mal_id:5114``. Derived from the shared external id
    rather than assigned, so re-running the linker cannot invent a new identity
    for a group it has already seen.
    """
    return f"work:{domain}:{key}:{value}"


def make_user_uid(source: str, user_id: str) -> str:
    """Namespace a user id by the dataset it came from.

    MAL usernames, MovieLens user numbers and Goodreads user numbers share one
    ``ratings`` table and collide freely otherwise -- MovieLens user ``21`` is
    not MAL user ``21``.
    """
    return f"{source}:{user_id}"


#: You. Loom is a local desktop app with no accounts, so the person using it is
#: a constant rather than a session -- but it is namespaced through
#: ``make_user_uid`` like every other user id, because your ratings share the
#: ``ratings`` table with MAL's 106,402 users and a bare ``you`` would be a MAL
#: username collision waiting to happen.
LOCAL_USER_UID = make_user_uid("local", "you")


class Creator(BaseModel):
    """A person or organisation attached to an item.

    ``role`` is intentionally free-form: "studio" and "author" and "artist" are
    not the same concept, and forcing them into one enum loses information.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    role: str | None = None

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("creator name must not be empty")
        return v


class UnifiedItem(BaseModel):
    """One catalog entry, normalised across sources.

    ``item_uid`` is derived, never supplied: it is always
    ``"{domain}:{source}:{source_id}"`` so it cannot drift from its parts.
    """

    model_config = ConfigDict(extra="forbid")

    item_uid: str = ""
    domain: Domain
    source: str
    source_id: str

    title: str
    alt_titles: list[str] = Field(default_factory=list)
    year: int | None = None
    creators: list[Creator] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    description: str | None = None
    external_ids: dict[str, str] = Field(default_factory=dict)

    # Source-relative "how big is this thing" signal (Jikan: member count).
    # NOT comparable across sources; used only for within-source ordering.
    popularity: float | None = None
    # Normalised to a 0-10 scale by each ingester, so it IS cross-source comparable.
    mean_rating: float | None = None
    n_ratings: int | None = None

    image_url: str | None = None
    fetched_at: datetime = Field(default_factory=utcnow)

    # Verbatim source payload, kept so a mapping bug can be fixed by re-running
    # the mapper over the DB instead of re-hitting the upstream API.
    raw_json: dict[str, Any] | None = None

    # ---- validation -------------------------------------------------------

    @field_validator("source", "source_id", "title", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> Any:
        # Jikan hands out int ids; MusicBrainz hands out UUIDs. Normalise early.
        return str(v) if v is not None and not isinstance(v, str) else v

    @field_validator("source", "source_id", "title")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("year")
    @classmethod
    def _sane_year(cls, v: int | None) -> int | None:
        if v is not None and not (1000 <= v <= 2100):
            raise ValueError(f"implausible year: {v}")
        return v

    @field_validator("mean_rating")
    @classmethod
    def _rating_range(cls, v: float | None) -> float | None:
        if v is not None and not (0.0 <= v <= 10.0):
            raise ValueError(f"mean_rating must be normalised to 0-10, got {v}")
        return v

    @field_validator("n_ratings")
    @classmethod
    def _non_negative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("n_ratings must be >= 0")
        return v

    @field_validator("tags")
    @classmethod
    def _norm_tags(cls, v: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for t in v:
            t = t.strip().lower()
            if t:
                seen.setdefault(t, None)
        return list(seen)

    @field_validator("alt_titles")
    @classmethod
    def _norm_alt(cls, v: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for t in v:
            t = t.strip()
            if t:
                seen.setdefault(t, None)
        return list(seen)

    @field_validator("external_ids", mode="before")
    @classmethod
    def _stringify_ids(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {str(k): str(val) for k, val in v.items() if val is not None}
        return v

    @field_validator("fetched_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)

    @model_validator(mode="after")
    def _derive_uid(self) -> UnifiedItem:
        expected = make_item_uid(self.domain, self.source, self.source_id)
        if self.item_uid and self.item_uid != expected:
            raise ValueError(f"item_uid {self.item_uid!r} does not match {expected!r}")
        object.__setattr__(self, "item_uid", expected)
        return self

    # ---- persistence helpers ---------------------------------------------

    def to_row(self) -> dict[str, Any]:
        """Flatten to the SQLite column shape (JSON fields become TEXT)."""
        return {
            "item_uid": self.item_uid,
            "domain": self.domain,
            "source": self.source,
            "source_id": self.source_id,
            "title": self.title,
            "alt_titles": json.dumps(self.alt_titles, ensure_ascii=False),
            "year": self.year,
            "creators": json.dumps([c.model_dump() for c in self.creators], ensure_ascii=False),
            "tags": json.dumps(self.tags, ensure_ascii=False),
            "description": self.description,
            "external_ids": json.dumps(self.external_ids, ensure_ascii=False),
            "popularity": self.popularity,
            "mean_rating": self.mean_rating,
            "n_ratings": self.n_ratings,
            "image_url": self.image_url,
            "fetched_at": self.fetched_at.isoformat(),
            "raw_json": json.dumps(self.raw_json, ensure_ascii=False) if self.raw_json else None,
        }

    @classmethod
    def from_row(cls, row: Any) -> UnifiedItem:
        d = dict(row)
        d["alt_titles"] = json.loads(d.get("alt_titles") or "[]")
        d["creators"] = json.loads(d.get("creators") or "[]")
        d["tags"] = json.loads(d.get("tags") or "[]")
        d["external_ids"] = json.loads(d.get("external_ids") or "{}")
        raw = d.get("raw_json")
        d["raw_json"] = json.loads(raw) if raw else None
        d.pop("rowid", None)
        return cls.model_validate(d)


# ---- API response models -------------------------------------------------


class ItemsPage(BaseModel):
    """Paginated /api/items response."""

    total: int
    limit: int
    offset: int
    items: list[UnifiedItem]


class TagCount(BaseModel):
    """One tag and how many works carry it -- a row of /api/tags."""

    tag: str
    n: int


class SimilarItem(BaseModel):
    """One neighbour, with the catalog row so a caller needs no second request."""

    score: float
    item: UnifiedItem


class SimilarPage(BaseModel):
    """/api/similar response.

    ``n_suppressed`` is reported rather than hidden: neighbours dropped for
    sharing a franchise with the query are the difference between a
    recommendation and a table of contents, and a caller comparing results
    should be able to see how many the filter took. It counts what was skipped
    on the way to filling this page, not the size of the franchise.
    """

    item_uid: str
    domain: str | None = None
    method: str = "content"
    n_suppressed: int = 0
    neighbours: list[SimilarItem]


class RatingIn(BaseModel):
    """A rating you are setting on a work.

    1-10, the scale every ingester already rescales ``mean_rating`` onto and
    the one MAL's 19.2M ratings use -- so your scores and the corpus's are
    directly comparable rather than needing a second calibration the way raw
    ``popularity`` did.
    """

    rating: float = Field(ge=1.0, le=10.0)


class RatedItem(BaseModel):
    """One entry in your library: your score, plus the catalog row it is on."""

    rating: float
    rated_at: datetime | None = None
    item: UnifiedItem


class LibraryPage(BaseModel):
    """Paginated /api/library response -- the things you have rated."""

    total: int
    limit: int
    offset: int
    items: list[RatedItem]


class RecommendPage(BaseModel):
    """/api/recommend response -- works nearest your taste, not near one item.

    ``n_rated`` and ``n_used`` are reported rather than hidden because they are
    the difference between a recommendation and a coin toss: a profile built
    from two ratings is not the same claim as one built from two hundred.

    ``n_used`` is how many of those ratings actually shaped the profile -- the
    ones the embedding matrix could place *and* that sat at or above your own
    average. Two causes, one number, and they are not distinguishable from the
    outside: a caller can say the profile is thinner than the rating count
    suggests, but not why.
    """

    domain: str | None = None
    n_rated: int
    n_used: int
    n_suppressed: int = 0
    neighbours: list[SimilarItem]


class DomainStats(BaseModel):
    domain: str
    n_items: int
    sources: list[str]
    last_ingest_at: datetime | None = None


class Stats(BaseModel):
    total_items: int
    by_domain: list[DomainStats]
    last_ingest_at: datetime | None = None
    db_path: str
    region: str


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    db_ready: bool
    n_items: int
