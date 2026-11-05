"""Putting every source's popularity on one scale, by calibrating on overlap.

The case that decides the whole design is `test_a_top_slice_source_is_not_sent
_to_the_bottom`. Percentile-within-source -- the obvious fix, and the one this
project's roadmap prescribed for a year -- fails it: ranking a top-25-only
source among its own rows sends its least popular row, still a top-25 title, to
percentile zero. Calibrating on shared works does not, because it never assumes
a source's rows are a representative sample.
"""

from __future__ import annotations

import pytest

from recsys.db import connect, init_db, query_items, upsert_items
from recsys.link import rebuild_links
from recsys.popularity import (
    MIN_ANCHORS,
    MIN_ANCHORS_FOR_CURVE,
    apply_curve,
    fit_curve,
    rebuild_popularity,
)
from recsys.schemas import UnifiedItem


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "pop.db"
    init_db(path)
    c = connect(path)
    yield c
    c.close()


def item(source, mal_id, popularity, *, title=None, domain="anime", tags=()):
    return UnifiedItem(
        domain=domain, source=source, source_id=str(mal_id),
        title=title or f"Work {mal_id}", popularity=popularity,
        tags=list(tags), external_ids={"mal_id": str(mal_id)},
    )


def seed(conn, *items_):
    upsert_items(conn, list(items_))
    rebuild_links(conn)
    return rebuild_popularity(conn)


def value(conn, uid):
    row = conn.execute("SELECT value FROM popularity_norm WHERE item_uid=?", (uid,)).fetchone()
    return row["value"] if row else None


def scale_for(result, source):
    return next(s for s in result.scales if s.source == source)


# ---- the reference --------------------------------------------------------


def test_the_broadest_source_becomes_the_reference(conn):
    """Chosen from the data. Naming a preferred source would mean editing the
    module to add a source, which the project invariant forbids."""
    result = seed(
        conn,
        *[item("big", i, 100.0 + i) for i in range(1, 6)],
        *[item("small", 1, 500.0)],
    )
    assert scale_for(result, "big").method == "reference"
    assert scale_for(result, "small").method != "reference"


def test_the_reference_keeps_its_raw_numbers(conn):
    seed(conn, *[item("big", i, 100.0 + i) for i in range(1, 6)])
    assert value(conn, "anime:big:1") == 101.0


def test_a_lone_source_in_a_new_domain_defines_its_own_scale(conn):
    """Books arriving with no anime to calibrate against must just work."""
    result = seed(
        conn,
        *[item("mal", i, 1000.0 * i) for i in range(1, 4)],
        *[item("goodreads", i, 7.0 * i, domain="book") for i in range(1, 4)],
    )
    assert scale_for(result, "goodreads").method == "reference"
    assert value(conn, "book:goodreads:1") == 7.0


# ---- calibration ----------------------------------------------------------


def test_a_source_on_a_doubled_scale_is_mapped_back(conn):
    """The whole point: two counts of the same works, one twice the other."""
    n = MIN_ANCHORS_FOR_CURVE + 5
    seed(
        conn,
        *[item("ref", i, float(1000 + i * 10)) for i in range(1, n + 1)],
        *[item("dbl", i, float(2 * (1000 + i * 10))) for i in range(1, n + 1)],
    )
    for i in (1, n // 2, n):
        ref = value(conn, f"anime:ref:{i}")
        assert value(conn, f"anime:dbl:{i}") == pytest.approx(ref, rel=0.02)


def test_calibration_preserves_a_source_own_ordering(conn):
    """The mapping is monotonic, so it may move a source relative to others but
    must never reshuffle it internally."""
    n = MIN_ANCHORS_FOR_CURVE + 5
    seed(
        conn,
        *[item("ref", i, float(100 * i)) for i in range(1, n + 1)],
        *[item("other", i, float(7 * i + 3)) for i in range(1, n + 1)],
    )
    vals = [value(conn, f"anime:other:{i}") for i in range(1, n + 1)]
    assert vals == sorted(vals)


def test_enough_anchors_fits_a_curve(conn):
    n = MIN_ANCHORS_FOR_CURVE + 1
    result = seed(
        conn,
        *[item("ref", i, float(100 * i)) for i in range(1, n + 1)],
        *[item("other", i, float(50 * i)) for i in range(1, n + 1)],
    )
    s = scale_for(result, "other")
    assert s.method == "curve" and s.n_anchors == n and len(s.knots) > 1


def test_few_anchors_fit_an_offset_only(conn):
    """A curve from three points is noise; a median log ratio is not."""
    n = MIN_ANCHORS + 1
    result = seed(
        conn,
        *[item("ref", i, float(100 * i)) for i in range(1, 40)],
        *[item("other", i, float(300 * i)) for i in range(1, n + 1)],
    )
    s = scale_for(result, "other")
    assert s.method == "offset"
    assert value(conn, "anime:other:1") == pytest.approx(100.0, rel=0.05)


def test_an_outlier_does_not_drag_the_offset(conn):
    """The offset is a median of log ratios precisely so one bad pair cannot."""
    result = seed(
        conn,
        *[item("ref", i, 100.0) for i in range(1, 40)],
        item("other", 1, 200.0), item("other", 2, 200.0),
        item("other", 3, 200.0), item("other", 4, 999999.0),
    )
    assert scale_for(result, "other").method == "offset"
    assert value(conn, "anime:other:1") == pytest.approx(100.0, rel=0.05)


# ---- the curve itself -----------------------------------------------------


def test_the_curve_is_forced_non_decreasing():
    """Real anchors can dip where two audiences disagree. A mapping that went
    backwards would reorder a source against itself, so dips are flattened."""
    xs = [1.0, 2.0, 3.0, 4.0]
    ys = [1.0, 5.0, 2.0, 6.0]  # third point dips
    knots = fit_curve(xs * 60, ys * 60)
    heights = [y for _, y in knots]
    assert heights == sorted(heights)


def test_the_curve_recovers_a_shape_a_power_law_cannot():
    """The reason this replaced the log-linear fit: no assumed model shape."""
    xs = [float(i) for i in range(1, 601)]
    ys = [x if x < 300 else 300 + (x - 300) * 5 for x in xs]  # elbow
    knots = fit_curve(xs, ys)
    for probe in (100.0, 250.0, 450.0):
        expected = probe if probe < 300 else 300 + (probe - 300) * 5
        assert apply_curve(knots, probe) == pytest.approx(expected, rel=0.05)


def test_the_curve_extrapolates_beyond_its_anchors():
    """A new source may carry works more popular than anything it shares with
    the reference; clamping would tie them all at the last knot."""
    xs = [float(i) for i in range(1, 401)]
    knots = fit_curve(xs, [x + 10 for x in xs])
    assert apply_curve(knots, 900.0) == pytest.approx(910.0, rel=0.05)
    assert apply_curve(knots, -50.0) == pytest.approx(-40.0, rel=0.05)


def test_an_empty_or_single_anchor_curve_is_harmless():
    assert apply_curve([], 5.0) == 5.0
    assert apply_curve([(2.0, 7.0)], 5.0) == 10.0


# ---- the case percentile ranking gets wrong -------------------------------


def test_a_top_slice_source_is_not_sent_to_the_bottom(conn):
    """The regression that motivates calibration over percentile ranking.

    `top` carries only the five most popular works, on a 5x scale. Ranking its
    rows among themselves would put its weakest at percentile 0 -- dead last in
    a 100-work catalog -- when it is genuinely the 5th most popular thing there
    is. Calibration has to leave all five at the top.
    """
    ref = [item("ref", i, float(i)) for i in range(1, 101)]
    top = [item("top", i, float(5 * i)) for i in range(96, 101)]
    seed(conn, *ref, *top)

    _, listing = query_items(conn, limit=10)
    top_uids = {f"anime:top:{i}" for i in range(96, 101)}
    # They are primary (more populated rows lose nothing here), so they should
    # occupy the head of the listing, not the tail.
    assert len({i.item_uid for i in listing} & top_uids) == 5


def test_a_top_slice_source_lands_near_its_true_level(conn):
    ref = [item("ref", i, float(i)) for i in range(1, 101)]
    top = [item("top", i, float(5 * i)) for i in range(96, 101)]
    seed(conn, *ref, *top)
    # work 100 is the single most popular thing in the reference scale
    assert value(conn, "anime:top:100") == pytest.approx(100.0, rel=0.15)


def test_no_overlap_falls_back_and_says_so(conn):
    """Nothing shared means nothing to calibrate on. The fallback reintroduces
    the assumption this module avoids, so it must be reported, not silent."""
    result = seed(
        conn,
        *[item("ref", i, float(i)) for i in range(1, 50)],
        *[item("island", 900 + i, float(10_000 * i)) for i in range(1, 5)],
    )
    s = scale_for(result, "island")
    assert (s.method, s.n_anchors) == ("quantile", 0)
    assert [f.source for f in result.fallbacks] == ["island"]
    # Mapped onto the reference's range rather than left on its own scale.
    assert value(conn, "anime:island:901") <= 49.0


# ---- how it reaches reads -------------------------------------------------


def test_ordering_uses_the_calibrated_value(conn):
    """A big raw number on an inflated scale must not outrank a smaller one."""
    n = MIN_ANCHORS_FOR_CURVE + 5
    seed(
        conn,
        *[item("ref", i, float(100 * i), title=f"Ref {i}") for i in range(1, n + 1)],
        # same works, numbers 100x larger, plus one genuinely middling entry
        *[item("loud", i, float(10_000 * i), title=f"Loud {i}") for i in range(1, n + 1)],
    )
    _, listing = query_items(conn, limit=5)
    # The most popular work wins regardless of which source represents it, and
    # the calibrated values -- not the 100x raw ones -- decide the order.
    assert listing[0].title.endswith(str(n))
    vals = [value(conn, i.item_uid) for i in listing]
    assert vals == sorted(vals, reverse=True)


def test_uncalibrated_rows_fall_back_to_the_raw_count(conn):
    """A fresh database, or --no-link: ordering degrades, never collapses."""
    upsert_items(conn, [item("mal", 1, 10.0), item("mal", 2, 900.0)])
    assert conn.execute("SELECT COUNT(*) FROM popularity_norm").fetchone()[0] == 0
    _, listing = query_items(conn)
    assert [i.item_uid for i in listing] == ["anime:mal:2", "anime:mal:1"]


def test_search_results_are_ordered_by_the_calibrated_value(conn):
    n = MIN_ANCHORS_FOR_CURVE + 2
    seed(
        conn,
        *[item("ref", i, float(100 * i), title=f"Bebop {i}") for i in range(1, n + 1)],
        *[item("loud", i, float(9999 * i), title=f"Bebop {i}") for i in range(1, n + 1)],
    )
    total, items = query_items(conn, q="bebop")
    assert total == n
    vals = [value(conn, i.item_uid) for i in items]
    assert vals == sorted(vals, reverse=True)


# ---- rebuilds -------------------------------------------------------------


def test_rebuilding_is_idempotent(conn):
    args = [item("ref", i, float(100 * i)) for i in range(1, 30)]
    args += [item("other", i, float(3 * i)) for i in range(1, 30)]
    seed(conn, *args)
    first = value(conn, "anime:other:5")
    rebuild_popularity(conn)
    assert value(conn, "anime:other:5") == first


def test_rows_without_popularity_are_left_alone(conn):
    seed(conn, item("ref", 1, 100.0), item("ref", 2, None))
    assert value(conn, "anime:ref:2") is None
    assert conn.execute("SELECT COUNT(*) FROM popularity_norm").fetchone()[0] == 1


def test_raw_popularity_is_never_modified(conn):
    """The calibrated number is a second opinion stored beside the source datum,
    never a replacement for it."""
    n = MIN_ANCHORS_FOR_CURVE + 2
    seed(
        conn,
        *[item("ref", i, float(100 * i)) for i in range(1, n + 1)],
        *[item("other", i, float(5 * i)) for i in range(1, n + 1)],
    )
    raw = conn.execute("SELECT popularity FROM items WHERE item_uid='anime:other:3'").fetchone()
    assert raw["popularity"] == 15.0
    assert value(conn, "anime:other:3") != 15.0


def test_the_anchor_query_walks_links_rather_than_scanning_the_domain(tmp_path):
    """Regression: comparing the two rows' domains inside the join let SQLite
    drive the reference side from the domain index -- every reference row for
    every source row, quadratic, and more than ten minutes on a full-size
    catalog. The plan must reach the reference row through `links`."""
    import recsys.popularity as popularity
    from recsys.db import connect, init_db

    init_db(tmp_path / "plan.db")
    conn = connect(tmp_path / "plan.db")
    captured = []
    real_execute = conn.execute

    class Spy:
        def execute(self, sql, params=()):
            captured.append((sql, params))
            return real_execute(sql, params)

    popularity._anchors(Spy(), "anime", "anilist", "mal")
    sql, params = captured[0]
    plan = " | ".join(r[3] for r in real_execute(f"EXPLAIN QUERY PLAN {sql}", params))
    assert "idx_links_work" in plan, plan
    assert plan.count("idx_items_domain_pop") <= 1, plan
    conn.close()
