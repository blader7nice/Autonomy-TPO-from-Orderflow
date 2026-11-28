"""Phase 3: features, baselines, detectors, dynamic series, touches, trades, statistics."""

from pathlib import Path

import numpy as np
import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from polars.testing import assert_frame_equal

from flowlevels.config import load_config
from flowlevels.footprint.bars import aggregate_footprint, ladder, summarize_bar
from flowlevels.footprint.minute import footprint_from_trades, ohlcv_from_trades
from flowlevels.levels.engine import Level, Zone, dynamic_levels
from flowlevels.research.backtest import signal_bar
from flowlevels.research.dynamic import DYNAMIC_NAMES, dynamic_series
from flowlevels.research.stats import mde, paired_uplift
from flowlevels.research.touches import SessionZones, _assign, static_touches
from flowlevels.research.trades import Costs, Path1m, simulate
from flowlevels.signals.detectors import DETECTORS, detector_flags
from flowlevels.signals.features import add_baselines, bar_features
from tests.helpers import T0, synthetic_trades

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "configs/default.yaml")
SC = CFG.symbol("BTCUSDT")
DAY = 86_400_000
TAPE = synthetic_trades(30_000, span_ms=3 * DAY, first_id=1)
FP1 = footprint_from_trades(TAPE, 0.1, 10.0)
OH1 = ohlcv_from_trades(TAPE)


# ------------------------------------------------------------------ features
@pytest.mark.parametrize("res", ["1m", "5m", "15m"])
def test_vectorized_stacked_imbalances_match_summarize_bar(res: str) -> None:
    fc = CFG.footprint.model_copy(update={"imb_ratio": 1.5, "stack_n": 2})
    dense = synthetic_trades(60_000, seed=11, span_ms=6 * 3_600_000)
    fp1 = footprint_from_trades(dense, 0.1, 10.0)
    bars = bar_features(ohlcv_from_trades(dense), fp1, res, fc)
    fp = aggregate_footprint(fp1, res)
    n_checked = n_stacked = 0
    for b in bars.sample(min(200, bars.height), seed=1).iter_rows(named=True):
        lad = ladder(fp.filter(pl.col("bar_ts") == b["bar_ts"]), b["bar_ts"])
        s = summarize_bar(lad, fc.imb_ratio, fc.imb_min_volume, fc.stack_n)
        mid = (lad.buckets[0] + lad.buckets[-1]) / 2
        exp_buy = any((lo + hi) / 2 <= mid for lo, hi in s.stacked_buy)
        exp_sell = any((lo + hi) / 2 >= mid for lo, hi in s.stacked_sell)
        assert b["stk_buy_low"] == exp_buy and b["stk_sell_high"] == exp_sell
        assert b["n_buckets"] == len(lad.buckets)
        assert b["poc_vol"] == pytest.approx(lad.total.max())
        assert b["low_vol"] == pytest.approx(lad.total[0])
        n_checked += 1
        n_stacked += exp_buy or exp_sell
    assert n_checked >= 24 and n_stacked > 5  # the comparison is not vacuous


def test_baselines_use_prior_same_kind_days_only() -> None:
    fc = CFG.footprint.model_copy(update={"baseline_days": 7, "baseline_min_samples": 2})
    # 2026-01-05 is a Monday. One bar per day at 00:00, volume = day index.
    days = list(range(14))
    t = [T0 + 4 * DAY + d * DAY for d in days]
    bars = pl.DataFrame({"bar_ts": t, "volume": [float(d) for d in days], "delta": [0.0] * 14})
    z = add_baselines(bars, fc)
    # Monday 2026-01-12 (d=7): prior 7 days are 01-05..01-11; weekdays 01-05..01-09 -> 0..4
    row = z.filter(pl.col("bar_ts") == t[7])
    med, mad = 2.0, 1.0
    assert row["volume_z"][0] == pytest.approx((7 - med) / (1.4826 * mad))
    # Saturday 2026-01-17 (d=12): prior weekend days 01-10, 01-11 -> 5, 6
    row = z.filter(pl.col("bar_ts") == t[12])
    assert row["volume_z"][0] == pytest.approx((12 - 5.5) / (1.4826 * 0.5))
    assert z["volume_z"][0] is None  # no history
    assert z["delta_z"].null_count() == 14  # MAD = 0 -> no baseline


# ------------------------------------------------------------------ detectors
def _bar(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "bar_ts": 0, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
        "volume": 100.0, "delta": 0.0, "n_minutes": 5, "poc_vol": 30.0, "low_vol": 10.0,
        "high_vol": 10.0, "stk_buy_low": False, "stk_sell_high": False, "n_buckets": 5,
        "close_pos": 0.5, "volume_z": 0.0, "delta_z": 0.0,
    }  # fmt: skip
    return base | kw


def _flags(rows: list[dict[str, object]]) -> dict[str, object]:
    df = pl.DataFrame([r | {"bar_ts": i * 300_000} for i, r in enumerate(rows)])
    return detector_flags(df, CFG.signals).row(-1, named=True)


# Each case: (detector, long bar sequence that triggers it, a near miss that must not).
_PRIOR = [_bar(low=99.5, high=101.0)] * 3
CASES = {
    "absorption": (
        [*_PRIOR, _bar(volume_z=2.0, delta=-30.0, close_pos=0.7)],
        [*_PRIOR, _bar(volume_z=1.0, delta=-30.0, close_pos=0.7)],
    ),
    "exhaustion": (
        [*_PRIOR, _bar(low=99.0, low_vol=2.0, poc_vol=30.0, close_pos=0.6)],
        [*_PRIOR, _bar(low=99.0, low_vol=6.0, poc_vol=30.0, close_pos=0.6)],
    ),
    "delta_divergence": (
        [*_PRIOR, _bar(low=99.0, delta=5.0)],
        [*_PRIOR, _bar(low=99.6, delta=5.0)],
    ),
    "stacked_imbalance": (
        [*_PRIOR, _bar(stk_buy_low=True, delta=10.0)],
        [*_PRIOR, _bar(stk_buy_low=True, delta=-10.0)],
    ),
    "trapped": (
        [*_PRIOR, _bar(delta_z=-2.0, close_pos=0.2, high=101.0), _bar(close=101.5, high=102.0)],
        [*_PRIOR, _bar(delta_z=-2.0, close_pos=0.2, high=101.0), _bar(close=100.9, high=102.0)],
    ),
}


@pytest.mark.parametrize("det", DETECTORS)
def test_detector_triggers_and_near_miss_does_not(det: str) -> None:
    hit, miss = CASES[det]
    assert _flags(hit)[f"{det}_long"] is True
    assert _flags(miss)[f"{det}_long"] is False


def _mirror(r: dict[str, object]) -> dict[str, object]:
    m = dict(r)
    for k in ("open", "high", "low", "close"):
        m[k] = 200.0 - float(r[k])  # type: ignore[arg-type]
    m["high"], m["low"] = m["low"], m["high"]
    m["low_vol"], m["high_vol"] = r["high_vol"], r["low_vol"]
    m["stk_buy_low"], m["stk_sell_high"] = r["stk_sell_high"], r["stk_buy_low"]
    m["delta"] = -float(r["delta"])  # type: ignore[arg-type]
    m["delta_z"] = -float(r["delta_z"])  # type: ignore[arg-type]
    m["close_pos"] = 1 - float(r["close_pos"])  # type: ignore[arg-type]
    return m


@pytest.mark.parametrize("det", DETECTORS)
def test_detectors_are_mirror_symmetric(det: str) -> None:
    hit, miss = CASES[det]
    assert _flags([_mirror(r) for r in hit])[f"{det}_short"] is True
    assert _flags([_mirror(r) for r in miss])[f"{det}_short"] is False
    assert _flags(hit)[f"{det}_short"] is False  # a long setup is not a short signal


def test_absorption_on_a_synthetic_tape() -> None:
    """Heavy selling into a bar that still closes high, built from trades end to end."""
    t0 = T0 + 600_000
    rows = [(t0 + i * 1000, 1000 + i, 100_000.0 - (i % 5) * 10, 0.5, "S", 1) for i in range(50)]
    rows += [(t0 + 60_000 + i, 2000 + i, 100_020.0, 0.1, "B", 1) for i in range(5)]
    tape = pl.DataFrame(rows, schema=TAPE.schema, orient="row")
    bars = bar_features(
        ohlcv_from_trades(tape), footprint_from_trades(tape, 0.1, 10.0), "5m", CFG.footprint
    )
    bars = bars.with_columns(pl.lit(3.0).alias("volume_z"), pl.lit(-3.0).alias("delta_z"))
    f = detector_flags(bars, CFG.signals).row(0, named=True)
    assert f["delta"] < 0 and f["close_pos"] == 1.0
    assert f["absorption_long"] and not f["absorption_short"]


def test_signal_bar_weighted_distinct_count() -> None:
    a, e = 1 << DETECTORS.index("absorption"), 1 << DETECTORS.index("exhaustion")
    masks = np.array([[a, a, e, 0], [0, 0, 0, a], [a | e, 0, 0, 0]])
    w = dict.fromkeys(DETECTORS, 1.0)
    assert signal_bar(masks, 4, 2.0, w).tolist() == [2, -1, 0]  # a twice counts once
    assert signal_bar(masks, 3, 1.0, w).tolist() == [0, -1, 0]  # bar 3 is outside W=3


# ------------------------------------------------------------------ dynamic levels
def test_dynamic_series_equals_engine() -> None:
    lc = CFG.levels
    dyn = dynamic_series(OH1, SC, lc, min_minutes=0)
    rng = np.random.default_rng(5)
    for t in rng.choice(OH1["minute_ts"].to_numpy()[100:], 25):
        row = dyn.filter(pl.col("minute_ts") == int(t)).row(0, named=True)
        eng = {lv.name: lv.price for lv in dynamic_levels(OH1, int(t), SC, lc)}
        for name in DYNAMIC_NAMES:
            if name in eng:
                assert row[name] == pytest.approx(eng[name], rel=1e-9), (t, name)
            else:
                assert row[name] is None, (t, name)


# ------------------------------------------------------------------ touches
def _zone(zid: str, lo: float, hi: float, anchor: float, score: float = 2.0) -> Zone:
    return Zone(zid, zid, score, anchor, lo, hi, (Level("A", "A", anchor),), "A")


def test_overlapping_zones_nearest_anchor_wins() -> None:
    # bar from 105 down to 96 (prev close 106): touch price = low 96
    lo, hi, pc = np.array([96.0]), np.array([105.0]), np.array([106.0])
    z_lo, z_hi = np.array([98.0, 94.0]), np.array([103.0, 99.0])
    anc = np.array([100.0, 97.0])
    assert _assign(lo, hi, pc, z_lo, z_hi, anc, np.array([5.0, 1.0])).tolist() == [1]
    # equal distance -> the higher score
    anc2 = np.array([95.0, 97.0])
    assert _assign(lo, hi, pc, z_lo, z_hi, anc2, np.array([1.0, 3.0])).tolist() == [1]
    assert _assign(
        np.array([110.0]), np.array([111.0]), pc, z_lo, z_hi, anc, np.ones(2)
    ).tolist() == [-1]


def test_touch_starts_counts_and_direction() -> None:
    d0 = T0
    closes = [110, 104, 103, 110, 104, 90, 96]
    lows = [109, 99, 100, 106, 99, 88, 89]
    highs = [111, 108, 105, 111, 107, 104, 97]
    bars = pl.DataFrame(
        {
            "bar_ts": [d0 + i * 300_000 for i in range(7)],
            "low": lows,
            "high": highs,
            "close": closes,
        },
        schema={"bar_ts": pl.Int64, "low": pl.Float64, "high": pl.Float64, "close": pl.Float64},
    )
    z = _zone("Z", 98.0, 102.0, 100.0)
    t = static_touches(bars, {d0: SessionZones(d0, 10.0, [z])}, "BTCUSDT", "5m")
    # bar1 enters from above (touch 1, long); bar2 stays (same touch); bar3 leaves;
    # bar4 re-enters (touch 2, long); bar5 continues inside the zone; bar6 is below (out).
    assert t["pos"].to_list() == [1, 4]
    assert t["touch_n"].to_list() == [1, 2] and t["direction"].to_list() == [1, 1]
    bars2 = bars.with_columns(pl.Series("close", [90.0, 95, 97, 90, 95, 90, 96]))
    t2 = static_touches(bars2, {d0: SessionZones(d0, 10.0, [z])}, "BTCUSDT", "5m")
    assert t2["direction"][0] == -1  # came from below -> short


# ------------------------------------------------------------------ trades
def _path(rows: list[tuple[float, float, float, float]]) -> Path1m:
    a = np.array(rows, float)
    return Path1m(T0 + 60_000 * np.arange(len(rows)), a[:, 0], a[:, 1], a[:, 2], a[:, 3])


NOCOST = Costs(0.0, 0.0, 0.0)


def test_stop_wins_when_stop_and_target_in_same_minute() -> None:
    p = _path([(100, 101, 99.5, 100), (100, 103, 97, 100)])
    f = simulate(p, T0, 1, 98.0, 0.0, "1R", None, 60, NOCOST)
    assert f.exit_kind == "stop" and f.net_r == pytest.approx(-1.0)


def test_target_time_stop_and_min_stop() -> None:
    p = _path([(100, 101, 99.5, 100), (100, 102.5, 99.5, 102), (102, 102.2, 101, 101.5)])
    f = simulate(p, T0, 1, 99.0, 0.0, "2R", None, 60, NOCOST)
    assert f.exit_kind == "target" and f.exit_px == 102.0 and f.gross_r == pytest.approx(2.0)
    f = simulate(p, T0, 1, 99.0, 0.0, "time", None, 3, NOCOST)
    assert f.exit_kind == "time" and f.exit_px == 101.5
    f = simulate(p, T0, 1, 99.9, 1.0, "1R", None, 60, NOCOST)  # stop widened to min 1.0
    assert f.r_unit == 1.0 and f.stop == 99.0 and f.exit_kind == "target"
    assert not simulate(p, T0, 1, 100.5, 0.0, "1R", None, 60, NOCOST).valid
    f = simulate(p, T0, 1, 99.0, 0.0, "zone", 99.0, 60, NOCOST)  # zone behind entry -> time
    assert f.exit_kind == "time"


def test_costs_in_r_and_bps() -> None:
    p = _path([(100, 100, 100, 100), (100, 104, 100, 104)])
    f = simulate(p, T0, 1, 98.0, 0.0, "2R", None, 60, Costs(0.5, 0.001, 0.001))
    net = (104 - 0.5) - (100 + 0.5) - 0.001 * 100.5 - 0.001 * 103.5
    assert f.net_r == pytest.approx(net / 2.0) and f.net_bps == pytest.approx(net / 100 * 1e4)
    s = simulate(
        _path([(100, 100, 100, 100), (100, 100, 90, 95)]),
        T0,
        -1,
        102.0,
        0.0,
        "1R",
        None,
        60,
        NOCOST,
    )
    assert s.exit_kind == "target" and s.gross_r == pytest.approx(1.0)  # short side


# ------------------------------------------------------------------ statistics
def test_paired_uplift_is_deterministic_and_clustered() -> None:
    rng = np.random.default_rng(0)
    rt, ru = rng.normal(0.3, 1, 400), rng.normal(0.0, 1, 400)
    st_, su = rng.integers(0, 40, 400), rng.integers(0, 40, 400)
    a = paired_uplift(rt, st_, ru, su, 500, 0.9, 1)
    b = paired_uplift(rt, st_, ru, su, 500, 0.9, 1)
    assert a == b and a.lo < a.uplift < a.hi and a.n_sessions == 40
    assert a.uplift == pytest.approx(rt.mean() - ru.mean())
    assert mde(0.1, 100, 400, 0.9, 0.8) == pytest.approx((1.6449 + 0.8416) * 0.05, rel=1e-3)


# ------------------------------------------------------------------ no lookahead
@settings(max_examples=15, deadline=None)
@given(st.integers(min_value=T0 + DAY, max_value=T0 + 3 * DAY - 1))
def test_bar_features_and_flags_do_not_look_ahead(t: int) -> None:
    """Features/flags of bars closed by t are the same with data truncated at t."""
    res, step = "5m", 300_000
    t = t // step * step
    tape = TAPE.filter(pl.col("ts_ms") < t)
    full = detector_flags(
        add_baselines(bar_features(OH1, FP1, res, CFG.footprint), CFG.footprint), CFG.signals
    )
    trunc = detector_flags(
        add_baselines(
            bar_features(
                ohlcv_from_trades(tape), footprint_from_trades(tape, 0.1, 10.0), res, CFG.footprint
            ),
            CFG.footprint,
        ),
        CFG.signals,
    )
    a = full.filter(pl.col("bar_ts") + step <= t)
    b = trunc.filter(pl.col("bar_ts") + step <= t)
    assert a.height == b.height
    # Equal up to float summation order (polars sums are not bit-reproducible, D-017).
    assert_frame_equal(a, b, check_exact=False, rel_tol=1e-9, abs_tol=1e-9)
