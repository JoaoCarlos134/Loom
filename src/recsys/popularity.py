"""Putting every source's popularity count on one scale.

``items.popularity`` is a raw audience number and each source counts something
different: MAL members frozen in 2018, MAL members live in 2026, AniList users
who have it on a list. Sorting on it directly compares yardsticks -- Jikan's
*least* popular row (160,982) outranked AniList's most popular (1,048,042)
purely on vintage and definition.

**Why not percentile-within-source.** The obvious fix -- rank each source's rows
among themselves -- assumes every source samples the same population. Jikan's
rows are the *top* 25 anime, so ranking them among themselves puts its least
popular row at percentile 0.0, "least popular in the catalog". That inverts the
error rather than fixing it, and any future top-N endpoint walks into the same
trap.

**What this does instead: calibrate on overlap.** Where two sources both carry
the same work -- which ``links`` already knows -- the pair of numbers is an
anchor. Fit a mapping from one source's scale onto the other's using those
anchors, and convert. This never assumes a source's rows are representative,
only that the works it shares with the reference are typical *of that source*.
Twenty-five overlapping works are plenty to fit one scaling factor.

**The mapping is a monotone curve, not a formula.** A power law was the obvious
model and measurably the wrong shape: fitted against 12,821 anime anchors it
overestimated the quiet end by 1.47x and underestimated the popular end by
0.74x, a two-fold tilt across the range that shows up as sources interleaving
wrongly. Binning the anchors and interpolating between their medians assumes
only that the relationship is increasing, which is the one thing that has to be
true. With too few anchors to bin, it falls back to a median scale factor.

Whatever the method, the mapping is monotonic, so a source's own ordering is
never disturbed -- only its position relative to other sources changes.

Everything here is derived. Raw ``popularity`` is never modified: it is the
honest source datum, and this is a second opinion stored beside it.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from dataclasses import dataclass, field
from typing import Sequence

from recsys.log import get_logger

log = get_logger(__name__)

#: Bins used for the monotone curve, and the anchors needed to justify it.
#: Ten anchors per bin keeps each bin's median from being noise.
CURVE_BINS = 20
MIN_ANCHORS_FOR_CURVE = CURVE_BINS * 10

#: Anchors needed to fit anything at all.
MIN_ANCHORS = 3


@dataclass
class SourceScale:
    domain: str
    source: str
    method: str  # "reference" | "curve" | "offset" | "quantile"
    n_rows: int
    n_anchors: int = 0
    offset: float = 0.0  # log-space scale factor, for the "offset" method
    knots: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class PopularityResult:
    n_scored: int = 0
    scales: list[SourceScale] = field(default_factory=list)

    @property
    def fallbacks(self) -> list[SourceScale]:
        return [s for s in self.scales if s.method == "quantile"]


def fit_curve(xs: Sequence[float], ys: Sequence[float]) -> list[tuple[float, float]]:
    """Knots of a monotone curve mapping source log-popularity to reference.

    Anchors are sorted by source value, split into equal-count bins, and each
    bin contributes the median of both coordinates. Medians rather than means
    because a single mismatched pair should not bend the curve.

    The knots are then forced non-decreasing. Real data can dip -- a bin where
    the two audiences genuinely disagree -- but a mapping that goes backwards
    would reorder a source against itself, which is never an improvement over
    simply flattening that step.
    """
    pairs = sorted(zip(xs, ys))
    per_bin = max(1, len(pairs) // CURVE_BINS)
    knots: list[tuple[float, float]] = []
    for start in range(0, len(pairs), per_bin):
        chunk = pairs[start : start + per_bin]
        if len(chunk) < per_bin // 2 and knots:
            break  # a short trailing bin would be noisier than the one before
        knots.append((
            statistics.median(x for x, _ in chunk),
            statistics.median(y for _, y in chunk),
        ))

    for i in range(1, len(knots)):
        if knots[i][1] < knots[i - 1][1]:
            knots[i] = (knots[i][0], knots[i - 1][1])
    return knots


def apply_curve(knots: list[tuple[float, float]], x: float) -> float:
    """Interpolate ``x`` through the curve, extending the end slopes beyond it.

    Extrapolation matters: a new source may carry works more or less popular
    than anything it shares with the reference, and clamping them all to the
    last knot would collapse the head or tail of its catalog into a tie.
    """
    if not knots:
        return x
    if len(knots) == 1:
        return x + (knots[0][1] - knots[0][0])

    if x <= knots[0][0]:
        lo, hi = knots[0], knots[1]
    elif x >= knots[-1][0]:
        lo, hi = knots[-2], knots[-1]
    else:
        i = max(j for j in range(len(knots)) if knots[j][0] <= x)
        lo, hi = knots[i], knots[min(i + 1, len(knots) - 1)]

    if hi[0] == lo[0]:
        return lo[1]
    slope = (hi[1] - lo[1]) / (hi[0] - lo[0])
    return lo[1] + slope * (x - lo[0])


def _quantile_map(values: list[float], reference: list[float]) -> dict[float, float]:
    """Map values onto the reference distribution by rank. The last resort.

    Used only when a source shares no work with the reference, which leaves
    nothing to calibrate against. It reintroduces the representative-sample
    assumption this module exists to avoid, so it is logged as a warning rather
    than done quietly.
    """
    if not reference:
        return {}
    ordered = sorted(set(values))
    ref = sorted(reference)
    out: dict[float, float] = {}
    span = max(len(ordered) - 1, 1)
    for i, v in enumerate(ordered):
        out[v] = ref[round(i / span * (len(ref) - 1))]
    return out


def _anchors(
    conn: sqlite3.Connection, domain: str, source: str, reference: str
) -> tuple[list[float], list[float]]:
    """Log-space (source, reference) pairs for works both sources carry.

    ``links`` is what makes this possible: it already knows which rows describe
    one work, so the anchors are exact rather than title-matched.
    """
    rows = conn.execute(
        """SELECT s.popularity AS ps, r.popularity AS pr
           FROM links ls
           JOIN items s ON s.item_uid = ls.item_uid AND s.source = ?
           JOIN links lr ON lr.work_uid = ls.work_uid
           JOIN items r ON r.item_uid = lr.item_uid AND r.source = ?
           WHERE s.domain = ? AND s.popularity > 0 AND r.popularity > 0""",
        (source, reference, domain),
    ).fetchall()
    return (
        [math.log(r["ps"]) for r in rows],
        [math.log(r["pr"]) for r in rows],
    )


def _share_within_works(
    conn: sqlite3.Connection, values: dict[str, float]
) -> dict[str, float]:
    """Give every row of a work the best figure any of its sources reported.

    Popularity describes the *work*, not the row that happens to represent it.
    Sources measure it with wildly different completeness -- Open Library
    carries no audience count at all, so a book row from there would sort below
    everything despite being the row with the synopsis, purely because it won
    the primary election. Taking the maximum across a work's rows says "this is
    how big this thing is, according to whichever source knows best", which is
    the honest reading.

    Necessary rather than cosmetic: ordering happens in SQL against the stored
    value, so filling the gap after the query would come too late to fix it.
    """
    members: dict[str, list[str]] = {}
    for item_uid, work_uid in conn.execute("SELECT item_uid, work_uid FROM links"):
        members.setdefault(work_uid, []).append(item_uid)

    shared = dict(values)
    for group in members.values():
        known = [values[uid] for uid in group if uid in values]
        if not known:
            continue
        best = max(known)
        for uid in group:
            shared[uid] = best
    return shared


def rebuild_popularity(conn: sqlite3.Connection) -> PopularityResult:
    """Recompute every calibrated popularity from scratch. Idempotent.

    Must run *after* ``link.rebuild_links``: the anchors come from ``links``.
    """
    result = PopularityResult()
    to_insert: list[tuple[str, float]] = []

    domains = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT domain FROM items WHERE popularity > 0 ORDER BY domain"
        )
    ]

    for domain in domains:
        by_source: dict[str, list[tuple[str, float]]] = {}
        for row in conn.execute(
            "SELECT item_uid, source, popularity FROM items "
            "WHERE domain = ? AND popularity > 0",
            (domain,),
        ):
            by_source.setdefault(row["source"], []).append((row["item_uid"], row["popularity"]))

        # The reference is whichever source covers the most of this domain,
        # decided from the data. Naming a preferred source here would mean
        # editing this file to add one, which the project invariant forbids.
        reference = max(by_source, key=lambda s: (len(by_source[s]), s))
        ref_values = [p for _, p in by_source[reference]]

        for source, rows in sorted(by_source.items()):
            scale = SourceScale(domain=domain, source=source, method="reference",
                                n_rows=len(rows))

            if source == reference:
                # The scale everything else is expressed in. Identity by
                # definition; a domain's first source defines its own scale.
                to_insert.extend((uid, pop) for uid, pop in rows)
                result.scales.append(scale)
                continue

            xs, ys = _anchors(conn, domain, source, reference)
            scale.n_anchors = len(xs)

            if len(xs) >= MIN_ANCHORS_FOR_CURVE:
                scale.method = "curve"
                scale.knots = fit_curve(xs, ys)
                to_insert.extend(
                    (uid, math.exp(apply_curve(scale.knots, math.log(pop))))
                    for uid, pop in rows
                )
            elif len(xs) >= MIN_ANCHORS:
                # Too few anchors to shape a curve; a pure scale shift is the
                # median of the log ratios, which one outlier cannot drag.
                scale.method = "offset"
                scale.offset = statistics.median(y - x for x, y in zip(xs, ys))
                to_insert.extend(
                    (uid, math.exp(math.log(pop) + scale.offset)) for uid, pop in rows
                )
            else:
                scale.method = "quantile"
                mapping = _quantile_map([p for _, p in rows], ref_values)
                to_insert.extend((uid, mapping.get(pop, pop)) for uid, pop in rows)
                log.warning(
                    "popularity: %s/%s shares no work with the reference %r -- "
                    "falling back to quantile mapping, which assumes its rows are "
                    "a representative sample",
                    domain, source, reference,
                )

            result.scales.append(scale)

        log.info("popularity: %s calibrated against %r", domain, reference)

    values = _share_within_works(conn, dict(to_insert))
    result.n_scored = len(values)

    with conn:  # one transaction: never leave the catalog half-calibrated
        conn.execute("DELETE FROM popularity_norm")
        conn.executemany(
            "INSERT INTO popularity_norm (item_uid, value) VALUES (?, ?)",
            list(values.items()),
        )

    for s in result.scales:
        log.info(
            "popularity: %s/%s %s (rows=%d anchors=%d knots=%d offset=%.3f)",
            s.domain, s.source, s.method, s.n_rows, s.n_anchors, len(s.knots), s.offset,
        )
    return result
