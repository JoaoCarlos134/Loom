"""MyAnimeList CSV dump (Kaggle: azathoth42/myanimelist) for the ``anime`` domain.

A file-backed ingester, added because the Jikan API degraded to serving only its
warm cache -- deep pagination, search, seasons and ordinary ``/anime/{id}``
lookups all return 504, capping the live catalog at 25 items. This dump carries
~14.5k anime and needs no upstream at all.

It is a *different source* from Jikan, not a replacement for it: rows land under
``anime:mal:{id}`` while Jikan's land under ``anime:jikan:{id}``, and both
populate ``external_ids["mal_id"]``. Two rows describing one work is the
documented Phase 0 behaviour -- reconciling them is Phase 2's job, and it needs
both rows to survive in order to do it.

Two things this dump does *not* have, worth knowing before trusting it:

* **No synopsis.** The closest column is ``background`` (production trivia),
  present on only ~7% of rows, so ``description`` is mostly empty and the FTS
  index leans almost entirely on titles here.
* **It is a static snapshot** (2018-era). Scores and member counts are frozen,
  and anything newer is simply absent. Re-running Jikan once it recovers is what
  refreshes the live half of the catalog.
"""

from __future__ import annotations

import ast
import csv
import re
import sys
from pathlib import Path
from typing import Any, AsyncIterator

from recsys.config import Settings
from recsys.ingest.base import Ingester
from recsys.ingest.kaggle import resolve_kaggle_file
from recsys.log import get_logger
from recsys.schemas import Creator, UnifiedItem, utcnow

log = get_logger(__name__)

SOURCE = "mal"
DOMAIN = "anime"

# `related` holds a serialised relation graph and can be long. The stdlib
# default (128 KB) is close enough to that to be worth lifting; an unbounded
# limit is not, since a corrupt file would then be read into memory whole.
csv.field_size_limit(10_000_000)

_YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")

# Placeholders this dump uses for "no value". Compared case-folded.
_NULLISH = {"", "unknown", "none", "null", "na", "n/a", "-"}


def _clean(value: Any) -> str | None:
    """Normalise a CSV cell to a real string or None."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.casefold() in _NULLISH else text


def _split_list(value: Any) -> list[str]:
    """Split a comma-separated cell. Empty parts dropped, order preserved."""
    text = _clean(value)
    if text is None:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _as_float(value: Any) -> float | None:
    text = _clean(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None


def _parse_year(raw: dict[str, Any]) -> int | None:
    """Recover the airing year from whichever column actually has it.

    Four columns can carry it and they disagree in shape, so they are tried in
    descending order of reliability.
    """
    # 1. anime_cleaned.csv ships a derived numeric column.
    year = _as_int(raw.get("aired_from_year"))
    if year is not None and 1000 <= year <= 2100:
        return year

    # 2. `aired` is a *Python* dict literal ("{'from': '1998-04-03', ...}"),
    #    not JSON -- single-quoted, so json.loads would reject it.
    #    literal_eval parses literals only and cannot execute code.
    aired = _clean(raw.get("aired"))
    if aired:
        try:
            parsed = ast.literal_eval(aired)
        except (ValueError, SyntaxError):
            parsed = None
        if isinstance(parsed, dict):
            match = _YEAR_RE.search(str(parsed.get("from") or ""))
            if match:
                return int(match.group(1))

    # 3. "Spring 1998" / 4. "Apr 3, 1998 to Apr 24, 1999"
    for key in ("premiered", "aired_string"):
        text = _clean(raw.get(key))
        if text:
            match = _YEAR_RE.search(text)
            if match:
                return int(match.group(1))
    return None


def _parse_score(raw: dict[str, Any]) -> float | None:
    """MAL scores are already 0-10, which is the unified scale.

    A stored 0 means "never rated", not "rated zero". Keeping it would tell the
    Phase 3 models that ~2% of the catalog is the worst anime ever made.
    """
    score = _as_float(raw.get("score"))
    if score is None or score <= 0.0 or score > 10.0:
        return None
    return score


def map_mal_row(raw: dict[str, Any], *, fetched_at: Any = None) -> UnifiedItem | None:
    """Map one CSV row to a UnifiedItem. None = unusable, skip it."""
    anime_id = _clean(raw.get("anime_id"))
    if anime_id is None:
        return None

    canonical = None
    for key in ("title", "title_english", "title_japanese"):
        candidate = _clean(raw.get(key))
        if candidate:
            canonical = candidate
            break
    if canonical is None:
        return None

    alt_titles = [
        t
        for key in ("title_english", "title_japanese")
        if (t := _clean(raw.get(key))) and t != canonical
    ]
    alt_titles += [t for t in _split_list(raw.get("title_synonyms")) if t != canonical]

    tags = _split_list(raw.get("genre"))
    if (media_type := _clean(raw.get("type"))) is not None:
        # Matches the Jikan mapper: TV / Movie / OVA is a useful cross-domain facet.
        tags.append(media_type)

    creators = [Creator(name=n, role="studio") for n in _split_list(raw.get("studio"))]
    creators += [Creator(name=n, role="producer") for n in _split_list(raw.get("producer"))]
    creators += [Creator(name=n, role="licensor") for n in _split_list(raw.get("licensor"))]

    external_ids = {
        "mal_id": anime_id,
        # Not a column in this dump, but the URL form is stable and it is the
        # join key Phase 2 will reconcile Jikan rows against.
        "mal_url": f"https://myanimelist.net/anime/{anime_id}",
    }

    scored_by = _as_int(raw.get("scored_by"))
    # `members` is the audience-size count. NOT the `popularity` column, which is
    # a *rank* (1 = most popular) and would invert every ordering built on it.
    members = _as_float(raw.get("members"))

    return UnifiedItem(
        domain=DOMAIN,
        source=SOURCE,
        source_id=anime_id,
        title=canonical,
        alt_titles=alt_titles,
        year=_parse_year(raw),
        creators=creators,
        tags=tags,
        # This dump has no synopsis; `background` is the nearest thing and is
        # present on ~7% of rows.
        description=_clean(raw.get("background")),
        external_ids=external_ids,
        popularity=members if members and members > 0 else None,
        mean_rating=_parse_score(raw),
        n_ratings=scored_by if scored_by and scored_by > 0 else None,
        image_url=_clean(raw.get("image_url")),
        fetched_at=fetched_at or utcnow(),
        raw_json=raw,
    )


class MalCsvAnimeIngester(Ingester):
    """Reads the anime table out of the Kaggle MyAnimeList dump."""

    name = "anime_mal_csv"
    domain = DOMAIN
    source = SOURCE

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.target_count = settings.mal_csv_target_count  # 0 = the whole file
        self.dataset = settings.mal_csv_dataset
        self.filename = settings.mal_csv_filename
        self.explicit_path = settings.mal_csv_path

    # ---- source file ------------------------------------------------------

    def resolve_path(self) -> Path:
        """Locate the CSV, downloading the dataset only if no path is configured."""
        return resolve_kaggle_file(
            dataset=self.dataset,
            filename=self.filename,
            explicit_path=self.explicit_path,
            setting_name="MAL_CSV_PATH",
        )

    # ---- Ingester contract ------------------------------------------------

    async def fetch(self) -> AsyncIterator[dict[str, Any]]:
        path = self.resolve_path()
        self._fetched_at = utcnow()
        log.info("reading %s", path)

        yielded = 0
        # newline="" per the csv docs (the reader handles line endings itself);
        # encoding is explicit because Python on Windows would otherwise open
        # this UTF-8 file as cp1252 and die on the first Japanese title.
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                yield row
                yielded += 1
                if self.target_count and yielded >= self.target_count:
                    break

        log.info("mal_csv: read %d rows from %s", yielded, path.name)

    def map_item(self, raw: dict[str, Any]) -> UnifiedItem | None:
        return map_mal_row(raw, fetched_at=getattr(self, "_fetched_at", None))
