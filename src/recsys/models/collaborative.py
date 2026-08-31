"""Item-item collaborative filtering over the ratings corpus.

The other half of the recommender, and the one the catalog cannot do. The
content model in ``text.py`` asks "what is this work *about*"; this asks "who
liked it, and what else did they like". They fail in different directions,
which is the reason to have both: content similarity happily returns a work
nobody enjoyed, and collaborative similarity has never heard of a work nobody
has rated.

**Why item-item and not a factorisation.** 19.2M ratings over 6,598 anime is a
short, wide problem: the item side is small enough that the exact item-item
matrix is 174 MB and takes minutes to build with two matmuls, while ALS would
add a dependency, a random seed, a rank to tune and an approximation, to solve
a problem that fits in memory exactly. Revisit if the ratings ever span 100k
items; at 6,598, exact is cheaper than approximate.

**Ratings live in a different uid space than listings, and that is the trap.**
Every rating points at ``anime:mal:{id}`` -- the Kaggle dump's rows -- but only
451 of the 6,598 rated rows are the ones that *represent* their work. The other
6,147 lost the primary election to an AniList row carrying a synopsis, so they
are exactly the uids ``query_items`` never returns and the embedding matrix
does not contain. A model built in rating space and read in listing space would
join to nothing and raise nothing, which is the failure ``CLAUDE.md`` warns
about for rating uids in general.

So the artifact is stored in **rating space**, which is its native one, and
translated at read time through ``links``. That keeps it valid across a
re-link: the model describes the corpus, and which row happens to represent a
work today is a separate decision that changes more often.

**Shrinkage is not optional here.** Two anime rated together by four people can
show a perfect correlation that means nothing. Every similarity is pulled
toward zero in proportion to how few users actually rated both, so a pair needs
evidence before it is allowed to be a neighbour.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from recsys.log import get_logger

log = get_logger(__name__)

#: Users are processed in blocks of this many so the dense user x item block
#: stays small. 1024 x 6,598 float32 is 27 MB; the accumulators it feeds are
#: 174 MB each, and those are the real memory floor.
USER_CHUNK = 1024


@dataclass
class CfResult:
    n_ratings: int = 0
    n_users: int = 0
    n_items: int = 0
    n_pairs: int = 0
    dropped_items: int = 0  # too few raters to say anything about
    build_seconds: float = 0.0
    neighbours_per_item: float = 0.0


@dataclass
class Neighbour:
    item_uid: str
    score: float


def _load_ratings(conn: sqlite3.Connection):
    """Ratings as three parallel arrays, plus the uid each index means.

    A full scan is the only intended read of this table -- it carries no
    secondary index for exactly that reason -- and it costs about ten seconds
    for 19.2M rows.
    """
    import numpy as np

    users: dict[str, int] = {}
    items: dict[str, int] = {}
    u_idx, i_idx, vals = [], [], []

    for user_uid, item_uid, rating in conn.execute(
        "SELECT user_uid, item_uid, rating FROM ratings"
    ):
        u = users.get(user_uid)
        if u is None:
            u = users[user_uid] = len(users)
        i = items.get(item_uid)
        if i is None:
            i = items[item_uid] = len(items)
        u_idx.append(u)
        i_idx.append(i)
        vals.append(rating)

    return (
        np.asarray(u_idx, dtype="int32"),
        np.asarray(i_idx, dtype="int32"),
        np.asarray(vals, dtype="float32"),
        [uid for uid, _ in sorted(items.items(), key=lambda kv: kv[1])],
    )


def build_collaborative(
    conn: sqlite3.Connection,
    *,
    out_path: Path,
    k: int = 50,
    min_raters: int = 20,
    shrinkage: float = 50.0,
) -> CfResult:
    """Build the item-item neighbour table and write it to parquet.

    ``min_raters`` drops items nobody has rated enough for the question to mean
    anything; ``shrinkage`` pulls a pair's similarity toward zero by
    ``n_common / (n_common + shrinkage)``, so agreement between four people
    cannot outrank agreement between four thousand.
    """
    import time

    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    started = time.perf_counter()
    result = CfResult()

    u_idx, i_idx, vals, uids = _load_ratings(conn)
    result.n_ratings = int(u_idx.size)
    result.n_users = int(u_idx.max()) + 1 if u_idx.size else 0
    n_items = len(uids)
    log.info("cf: %d ratings, %d users, %d items", result.n_ratings, result.n_users, n_items)

    # Centre on each user's own mean. A user who rates everything 8 and a user
    # who rates everything 4 are saying the same thing about relative
    # preference, and an uncentred cosine would read them as disagreeing.
    user_sum = np.bincount(u_idx, weights=vals, minlength=result.n_users)
    user_n = np.bincount(u_idx, minlength=result.n_users)
    user_mean = np.divide(user_sum, np.maximum(user_n, 1), dtype="float64").astype("float32")
    centred = vals - user_mean[u_idx]

    raters = np.bincount(i_idx, minlength=n_items)
    keep = raters >= min_raters
    result.dropped_items = int((~keep).sum())
    result.n_items = int(keep.sum())
    if result.n_items == 0:
        log.warning("cf: no item reached min_raters=%d; nothing to build", min_raters)
        return result

    # Reindex to the kept items so the accumulators are as small as possible.
    remap = np.full(n_items, -1, dtype="int32")
    remap[keep] = np.arange(result.n_items, dtype="int32")
    live = remap[i_idx] >= 0
    u_idx, centred = u_idx[live], centred[live]
    i_idx = remap[i_idx[live]]
    kept_uids = [uid for uid, k_ in zip(uids, keep, strict=True) if k_]

    # Order by user so each chunk is a contiguous slice.
    order = np.argsort(u_idx, kind="stable")
    u_idx, i_idx, centred = u_idx[order], i_idx[order], centred[order]
    starts = np.searchsorted(u_idx, np.arange(result.n_users + 1))

    dot = np.zeros((result.n_items, result.n_items), dtype="float32")
    common = np.zeros((result.n_items, result.n_items), dtype="float32")
    block = np.zeros((USER_CHUNK, result.n_items), dtype="float32")
    seen = np.zeros((USER_CHUNK, result.n_items), dtype="float32")

    for lo in range(0, result.n_users, USER_CHUNK):
        hi = min(lo + USER_CHUNK, result.n_users)
        a, b = starts[lo], starts[hi]
        if a == b:
            continue
        block[: hi - lo].fill(0.0)
        seen[: hi - lo].fill(0.0)
        rows = u_idx[a:b] - lo
        block[rows, i_idx[a:b]] = centred[a:b]
        seen[rows, i_idx[a:b]] = 1.0
        # Two accumulations: the dot products that become cosine, and the
        # co-rater counts that decide how much of that cosine to believe.
        dot += block[: hi - lo].T @ block[: hi - lo]
        common += seen[: hi - lo].T @ seen[: hi - lo]
        if (lo // USER_CHUNK) % 25 == 0:
            log.info("cf: %d/%d users accumulated", hi, result.n_users)

    norms = np.sqrt(np.maximum(np.diag(dot), 1e-9)).astype("float32")
    sim = dot / norms[:, None] / norms[None, :]
    sim *= common / (common + shrinkage)
    np.fill_diagonal(sim, -np.inf)  # a work is not its own recommendation

    take = min(k, result.n_items - 1)
    top = np.argpartition(-sim, take - 1, axis=1)[:, :take]
    rows = np.arange(result.n_items)[:, None]
    order_k = np.argsort(-sim[rows, top], axis=1)
    top = top[rows, order_k]
    scores = sim[rows, top]

    src, dst, sc = [], [], []
    for i in range(result.n_items):
        for j, s in zip(top[i], scores[i], strict=True):
            if s <= 0.0 or not np.isfinite(s):
                continue  # a negative neighbour is not a recommendation
            src.append(kept_uids[i])
            dst.append(kept_uids[int(j)])
            sc.append(float(s))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_suffix(out_path.suffix + ".part")
    pq.write_table(
        pa.table({
            "item_uid": pa.array(src, type=pa.string()),
            "neighbour_uid": pa.array(dst, type=pa.string()),
            "score": pa.array(sc, type=pa.float32()),
        }),
        partial,
        compression="zstd",
    )
    partial.replace(out_path)

    result.n_pairs = len(src)
    result.neighbours_per_item = result.n_pairs / result.n_items if result.n_items else 0.0
    result.build_seconds = time.perf_counter() - started
    log.info(
        "cf: %d pairs over %d items (%.1f each), %d items dropped, %.1fs",
        result.n_pairs, result.n_items, result.neighbours_per_item,
        result.dropped_items, result.build_seconds,
    )
    return result


class CollaborativeIndex:
    """Read side of the neighbour table, translated into listing uid space.

    The artifact is keyed on the uids the *ratings* use. Callers hold the uids
    the *catalog displays*. This class owns that translation and is the only
    place it happens, so a caller cannot accidentally ask in the wrong space
    and silently get nothing back.
    """

    def __init__(self, neighbours: dict[str, list[Neighbour]]) -> None:
        self.neighbours = neighbours

    def __len__(self) -> int:
        return len(self.neighbours)

    @classmethod
    def load(cls, path: Path) -> CollaborativeIndex:
        import pyarrow.parquet as pq

        table = pq.read_table(path)
        out: dict[str, list[Neighbour]] = {}
        for item_uid, neighbour_uid, score in zip(
            table.column("item_uid").to_pylist(),
            table.column("neighbour_uid").to_pylist(),
            table.column("score").to_pylist(),
            strict=True,
        ):
            out.setdefault(item_uid, []).append(Neighbour(neighbour_uid, float(score)))
        return cls(out)

    def has(self, item_uid: str) -> bool:
        return item_uid in self.neighbours

    def similar_to(
        self, conn: sqlite3.Connection, item_uid: str, *, k: int = 10
    ) -> list[Neighbour]:
        """Neighbours of a work, asked and answered in listing uid space.

        Both ends are translated: the query is resolved to whichever of the
        work's rows the ratings actually name, and each neighbour is resolved
        back to whichever row represents *its* work.
        """
        from recsys.link import work_members

        rated_uid = next(
            (m for m in work_members(conn, item_uid) if m in self.neighbours), None
        )
        if rated_uid is None:
            return []

        out: list[Neighbour] = []
        seen = {item_uid}
        for neighbour in self.neighbours[rated_uid]:
            display = _representative(conn, neighbour.item_uid)
            if display in seen:
                continue
            seen.add(display)
            out.append(Neighbour(display, neighbour.score))
            if len(out) == k:
                break
        return out


def _representative(conn: sqlite3.Connection, item_uid: str) -> str:
    """The row that represents ``item_uid``'s work, or itself if it stands alone.

    Delegates to ``link.representative_uid`` so this translation lives in exactly
    one place -- the same helper franchise suppression uses.
    """
    from recsys.link import representative_uid

    return representative_uid(conn, item_uid)


@dataclass
class CfRecallResult:
    """Held-out recall for the model and for the baseline it has to beat."""

    n_users: int = 0
    hits: dict[int, int] = field(default_factory=dict)
    popular_hits: dict[int, int] = field(default_factory=dict)

    def recall(self, k: int) -> float:
        return self.hits.get(k, 0) / self.n_users if self.n_users else 0.0

    def popular_recall(self, k: int) -> float:
        return self.popular_hits.get(k, 0) / self.n_users if self.n_users else 0.0


def held_out_recall(
    conn: sqlite3.Connection,
    index: CollaborativeIndex,
    *,
    cutoffs: tuple[int, ...] = (1, 10, 50),
    n_users: int = 2000,
    min_ratings: int = 20,
    liked: float = 8.0,
    seed: int = 0,
) -> CfRecallResult:
    """Hide one anime a user loved; see whether the model finds it again.

    **Against a popularity baseline, which is the point.** Recommending the
    most-rated anime to everybody scores surprisingly well on this test and is
    not personalisation at all -- it is the ratings equivalent of the shared
    titles that flatter the content model's recall. A collaborative model only
    earns its place by beating that column, so both are reported and the
    baseline is computed over exactly the same held-out users.
    """
    import random

    rng = random.Random(seed)
    result = CfRecallResult()
    result.hits = dict.fromkeys(cutoffs, 0)
    result.popular_hits = dict.fromkeys(cutoffs, 0)
    largest = max(cutoffs)

    popular = [
        r[0]
        for r in conn.execute(
            """SELECT item_uid FROM ratings GROUP BY item_uid
               ORDER BY COUNT(*) DESC LIMIT ?""",
            (largest + 200,),
        )
    ]

    candidates = [
        r[0]
        for r in conn.execute(
            "SELECT user_uid FROM ratings GROUP BY user_uid HAVING COUNT(*) >= ?",
            (min_ratings,),
        )
    ]
    rng.shuffle(candidates)

    for user_uid in candidates:
        if result.n_users >= n_users:
            break
        rated = conn.execute(
            "SELECT item_uid, rating FROM ratings WHERE user_uid = ?", (user_uid,)
        ).fetchall()
        loved = [r["item_uid"] for r in rated if r["rating"] >= liked and index.has(r["item_uid"])]
        if len(loved) < 2:
            continue

        target = loved[rng.randrange(len(loved))]
        profile = [u for u in loved if u != target]
        known = {r["item_uid"] for r in rated}

        # Score every neighbour of everything else the user loved, summing the
        # similarities -- an item several of their favourites point at ranks
        # above one that only a single favourite points at.
        scored: dict[str, float] = {}
        for source_uid in profile:
            for neighbour in index.neighbours.get(source_uid, ()):
                if neighbour.item_uid in known and neighbour.item_uid != target:
                    continue
                scored[neighbour.item_uid] = scored.get(neighbour.item_uid, 0.0) + neighbour.score

        ranked = sorted(scored, key=lambda u: -scored[u])
        baseline = [u for u in popular if u not in known or u == target]

        result.n_users += 1
        for k in cutoffs:
            if target in ranked[:k]:
                result.hits[k] += 1
            if target in baseline[:k]:
                result.popular_hits[k] += 1

    return result


def report_held_out_recall(conn: sqlite3.Connection, path: Path) -> int:
    """Print the evaluation. Returns a process exit code."""
    if not Path(path).is_file():
        print(f"No collaborative model at {path}. Run scripts/cf.py first.")
        return 1

    index = CollaborativeIndex.load(path)
    result = held_out_recall(conn, index)
    if not result.n_users:
        print("No users had two rated favourites in the model; nothing to score.")
        return 1

    print(f"\nHeld-out recall over {result.n_users} users, one hidden favourite each")
    print(f"{len(index)} items carry neighbours\n")
    print(f"  {'':<12}{'model':>10}{'most-rated':>14}")
    for k in (1, 10, 50):
        print(f"  recall@{k:<5}{100 * result.recall(k):>9.1f}%"
              f"{100 * result.popular_recall(k):>13.1f}%")
    print("\n  The right column is what recommending the most-rated anime to")
    print("  everybody achieves. Only the gap between the columns is the model.")
    return 0
