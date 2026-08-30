"""Scoring the content model against links nobody had to label.

A recommender is easy to build and hard to trust. Cross-domain similarity is
especially hard to judge by eye -- given an anime and a novel, "is this a good
suggestion" has no obvious answer, and a model that returns plausible-looking
nonsense is indistinguishable from one that works until someone checks.

The catalog already contains ground truth for exactly this. AniList records that
an anime was adapted from a particular manga: the same story in two media, a
cross-domain pair asserted by the source rather than inferred by us. If the
model cannot put *those* near each other, it will not connect an anime to an
unrelated book either, and any cross-domain recommendation it makes is
decoration.

So the measure is recall@k over adaptation pairs: for each anime with a known
manga source in the catalog, ask for the nearest manga and check whether the
real one is among them. Random guessing over ~29,000 manga scores about 0.03%
at k=10, so anything meaningfully above that is signal.

This is a floor, not a ceiling. An adaptation shares its plot, its characters
and often its synopsis wording, which makes it the *easiest* cross-domain pair
there is. Scoring well here does not prove the model can match a novel to an
anime on theme alone -- it only proves the model is not broken.

**And the headline number flatters itself**, which is why it is reported split.
87% of adaptation pairs share a title, and the document embeds the title, so
most of that recall is a dressed-up string match. The pairs whose titles
genuinely differ are the ones that measure whether the model understands
anything: measured at 66.5% recall@10 against 93.5% for the easy half. Both are
worth knowing; only the harder one is evidence.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from recsys.log import get_logger

log = get_logger(__name__)

#: Where recall is reported.
CUTOFFS = (1, 5, 10, 50)


#: Titles are compared with punctuation and case removed, and one containing
#: the other counts as the same -- "Steins;Gate" against "Steins Gate 0" is not
#: a test of anything.
_TITLE_NOISE = re.compile(r"[^a-z0-9]+")


def _same_title(a: str | None, b: str | None) -> bool:
    x, y = _TITLE_NOISE.sub("", (a or "").lower()), _TITLE_NOISE.sub("", (b or "").lower())
    if not x or not y:
        return False
    return x == y or x in y or y in x


@dataclass
class RecallResult:
    n_pairs: int = 0
    hits: dict[int, int] = field(default_factory=dict)
    n_manga: int = 0
    #: The same measurement over only the pairs whose titles differ.
    n_hard: int = 0
    hard_hits: dict[int, int] = field(default_factory=dict)

    def recall(self, k: int) -> float:
        return self.hits.get(k, 0) / self.n_pairs if self.n_pairs else 0.0

    def hard_recall(self, k: int) -> float:
        return self.hard_hits.get(k, 0) / self.n_hard if self.n_hard else 0.0

    @property
    def random_baseline(self) -> float:
        """What recall@10 a coin flip would achieve, for scale."""
        return 10 / self.n_manga if self.n_manga else 0.0


def adaptation_pairs(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(anime item_uid, manga item_uid) for adaptations both sides of which we hold.

    Only pairs where the anime row is the one representing its work, since that
    is the row the embedding matrix carries.
    """
    rows = conn.execute(
        """SELECT a.item_uid AS anime_uid,
                  CAST(json_extract(e.value, '$.node.id') AS INTEGER) AS manga_id
           FROM items a
           LEFT JOIN links la ON la.item_uid = a.item_uid,
                json_each(json_extract(a.raw_json, '$.relations.edges')) e
           WHERE a.domain = 'anime' AND a.source = 'anilist'
             AND (la.item_uid IS NULL OR la.is_primary = 1)
             AND json_extract(e.value, '$.node.type') = 'MANGA'
             AND json_extract(e.value, '$.relationType') IN ('ADAPTATION', 'SOURCE')"""
    ).fetchall()

    # Map AniList manga ids to whichever row represents that manga work.
    manga = {}
    for row in conn.execute(
        """SELECT m.source_id, m.item_uid
           FROM items m
           LEFT JOIN links lm ON lm.item_uid = m.item_uid
           WHERE m.domain = 'manga' AND m.source = 'anilist'
             AND (lm.item_uid IS NULL OR lm.is_primary = 1)"""
    ):
        manga[str(row["source_id"])] = row["item_uid"]

    pairs = []
    for row in rows:
        target = manga.get(str(row["manga_id"]))
        if target:
            pairs.append((row["anime_uid"], target))
    return pairs


def adaptation_recall(
    conn: sqlite3.Connection, embeddings_path: Path, cutoffs: tuple[int, ...] = CUTOFFS
) -> RecallResult:
    """Recall@k for finding an anime's source manga among all manga."""
    from recsys.models.similarity import SimilarityIndex

    index = SimilarityIndex.load(embeddings_path)
    result = RecallResult()
    result.n_manga = sum(1 for d in index.domains if d == "manga")
    result.hits = {k: 0 for k in cutoffs}
    result.hard_hits = {k: 0 for k in cutoffs}
    largest = max(cutoffs)

    titles = {r[0]: r[1] for r in conn.execute("SELECT item_uid, title FROM items")}

    for anime_uid, manga_uid in adaptation_pairs(conn):
        if not index.has(anime_uid) or not index.has(manga_uid):
            continue
        result.n_pairs += 1
        hard = not _same_title(titles.get(anime_uid), titles.get(manga_uid))
        if hard:
            result.n_hard += 1

        found = index.similar_to(anime_uid, k=largest, domain="manga")
        for rank, neighbour in enumerate(found, start=1):
            if neighbour.item_uid == manga_uid:
                for k in cutoffs:
                    if rank <= k:
                        result.hits[k] += 1
                        if hard:
                            result.hard_hits[k] += 1
                break

    return result


def report_adaptation_recall(conn: sqlite3.Connection, embeddings_path: Path) -> int:
    """Print the evaluation. Returns a process exit code."""
    if not Path(embeddings_path).is_file():
        print(f"No embedding matrix at {embeddings_path}. Run scripts/embed.py first.")
        return 1

    result = adaptation_recall(conn, embeddings_path)
    if not result.n_pairs:
        print("No adaptation pairs with both sides embedded; nothing to score.")
        return 1

    easy = result.n_pairs - result.n_hard
    print(f"\nAdaptation recall over {result.n_pairs} anime with a known manga source")
    print(f"searching {result.n_manga} manga\n")
    print(f"  {'':<12}{'all':>10}{'diff. title':>14}")
    print(f"  {'pairs':<12}{result.n_pairs:>10}{result.n_hard:>14}")
    for k in CUTOFFS:
        print(f"  recall@{k:<5}{100 * result.recall(k):>9.1f}%"
              f"{100 * result.hard_recall(k):>13.1f}%")
    print(f"\n  random baseline @10 would be {100 * result.random_baseline:.2f}%")
    print(f"\n  {easy} of these pairs share a title, and the document embeds the")
    print("  title, so the left column is partly a dressed-up string match.")
    print("  The right column is the column that is evidence.")
    return 0
