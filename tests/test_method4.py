"""ITERATION-4 (method4) unit tests: fractals, bias, swings, POI first test, sweep,
confirmation, zones, orderflow window, and a no-lookahead truncation property."""

from pathlib import Path

import numpy as np
import polars as pl

from flowlevels.config import load_config
from flowlevels.strategies import method4 as m4

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "configs/default.yaml")


def bars(o, h, lo, c, step=m4.M15, t0=0) -> m4.Bars:  # type: ignore[no-untyped-def]
    n = len(o)
    return m4.Bars(step, np.arange(n) * step + t0, *(np.asarray(x, float) for x in (o, h, lo, c)))


def test_fractals_strict() -> None:
    h = np.array([1, 2, 5, 2, 1, 1, 5, 5, 1, 1.0])
    lo = h - 0.5
    hi, _ = m4.fractals(h, lo, 2)
    assert list(np.flatnonzero(hi)) == [2]  # tie at 6/7 forms no swing


def test_bias_b2_confirmed_two_bars_later() -> None:
    # highs: swing highs at 2 (10) and 6 (12); lows: swing lows at 4 (5) and 8 (6)
    h = np.array([8, 9, 10, 9, 8, 11, 12, 11, 10, 11, 12, 13.0])
    lo = np.array([6, 7, 8, 6, 5, 7, 8, 7, 6, 8, 9, 10.0])
    b = bars(h, h, lo, h, step=m4.H4)
    out = m4.bias_b2(b, 2)
    # second swing low (bar 8) is confirmed at the close of bar 10
    assert out[9] == 0 and out[10] == 1


def test_swing_before_no_lookahead() -> None:
    lo = np.array([5, 4, 1, 4, 5, 6, 7, 8.0])
    b = bars(lo + 1, lo + 2, lo, lo + 1)
    _, sl = m4.swing_before(b, 2)
    assert np.isnan(sl[4]) and sl[5] == 1.0  # j=2 confirmed at close of 4 -> usable from C=5


def _poi_bars() -> m4.Bars:
    # swing low at bar 2 (99), then a sweep of level 100 and the swing at bar 6
    lo = [101, 100.5, 99, 100.5, 101, 101.5, 98.5, 100.8]
    h = [x + 1 for x in lo]
    o = [x + 0.5 for x in lo]
    c = [x + 0.6 for x in lo]
    c[6] = 100.4  # closes back above 100 in the sweep bar
    return bars(o, h, lo, c)


def test_scan_poi_sweep_same_bar() -> None:
    b = _poi_bars()
    sh, sl = m4.swing_before(b, 2)
    p = m4.POI(1, "day_val", 100.0, 100.0, int(b.ts[5]), int(b.ts[-1]) + m4.M15)
    s = m4.scan_poi(p, b, sh, sl, 0.1)
    assert s is not None and s.c_idx == 6 and s.k_idx == 6 and s.extreme == 98.5
    assert p.first_touch_ts == int(b.ts[6])


def test_scan_poi_next_bar_close_back_and_swing_required() -> None:
    b = _poi_bars()
    c = b.c.copy()
    c[6] = 99.5
    b2 = m4.Bars(b.step, b.ts, b.o, b.h, b.l, c)
    sh, sl = m4.swing_before(b2, 2)
    p = m4.POI(1, "day_val", 100.0, 100.0, int(b.ts[5]), int(b.ts[-1]) + m4.M15)
    s = m4.scan_poi(p, b2, sh, sl, 0.1)
    assert s is not None and (s.c_idx, s.k_idx) == (6, 7)
    # sweep that does not take out the 15m swing low (99) -> no setup
    lo = b.l.copy()
    lo[6] = 99.0
    b3 = m4.Bars(b.step, b.ts, b.o, b.h, lo, b.c)
    sh, sl = m4.swing_before(b3, 2)
    p = m4.POI(1, "day_val", 100.0, 100.0, int(b.ts[5]), int(b.ts[-1]) + m4.M15)
    assert m4.scan_poi(p, b3, sh, sl, 0.1) is None


def test_scan_poi_consumed_by_close_above_near_edge() -> None:
    b = _poi_bars()
    sh, sl = m4.swing_before(b, 2)
    # zone [99.8, 101.2]: bar 4 touches the top (low 101) and closes above it -> consumed
    p = m4.POI(1, "zone_1h", 101.2, 99.8, int(b.ts[4]), int(b.ts[-1]) + m4.M15)
    assert m4.scan_poi(p, b, sh, sl, 0.1) is None  # bar 4 touched (low 101 <= 101.2), closed 101.6


def test_zones_demand_and_supply() -> None:
    # base 0 (o 100, c 99, l 98.5), impulse 1 up, bar 2 low 101.5 > high of base 100.5
    b = bars([100, 99.2, 102], [100.5, 102, 103], [98.5, 99, 101.5], [99, 101.8, 102.5], step=m4.H1)
    zs = m4.zones(b, 168 * m4.H1, "zone_1h")
    assert len(zs) == 1
    z = zs[0]
    assert (z.direction, z.near, z.far, z.start) == (1, 100.0, 98.5, int(b.ts[2] + m4.H1))


def test_c1() -> None:
    b = bars([100, 100], [101, 101], [99, 99], [100.5, 100.8])
    assert m4.c1_ok(b, 1, 1, 2 / 3)  # (100.8 - 99) / 2 = 0.9
    assert not m4.c1_ok(b, 1, -1, 2 / 3)
    eng = bars([101, 99.9], [101.2, 102], [99.8, 99.0], [100, 101.5])
    assert m4.c1_ok(eng, 1, 1, 0.99)  # engulfing although not in the top 1 %


def test_orderflow_confirm_window() -> None:
    ts = np.array([0, m4.M30, 3 * m4.M30])
    of = m4.OrderflowFlags(ts, np.array([False, True, False]), np.zeros(3, bool), np.ones(3, bool))
    assert of.confirm(0, m4.M30 + m4.M15, 1) == (True, True)
    assert of.confirm(0, m4.M15, 1) == (False, True)
    assert of.confirm(m4.M30, 2 * m4.M30, 1) == (False, False)  # bar 2*M30 missing


def _random_klines(days: int, seed: int) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    n = days * 1440
    t0 = 1_735_689_600_000  # 2025-01-01
    r = rng.standard_t(4, n) * 15.0
    c = 90_000 + np.cumsum(r)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.exponential(8, n)
    lo = np.minimum(o, c) - rng.exponential(8, n)
    return pl.DataFrame(
        {
            "minute_ts": t0 + np.arange(n) * 60_000,
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "volume": rng.exponential(10, n),
        }
    )


def test_run_symbol_no_lookahead() -> None:
    m = _random_klines(70, 7)
    t0 = int(m["minute_ts"][0])
    start, end = t0 + 45 * m4.DAY, t0 + 70 * m4.DAY
    cfg = CFG.model_copy(update={"iteration4": CFG.iteration4.model_copy(update={"min_rr": [0.3]})})
    full, _ = m4.run_symbol(m, None, start, end, cfg, "BTCUSDT")
    assert full, "synthetic data should produce some c1 trades"
    cut = start + 15 * m4.DAY
    part, _ = m4.run_symbol(m.filter(pl.col("minute_ts") < cut), None, start, cut, cfg, "BTCUSDT")
    done_full = [r for r in full if r["exit_ts"] < cut - m4.MIN and r["entry_ts"] < cut]
    done_part = [r for r in part if not r["window_end_close"]]
    key = ("config", "entry_ts", "direction", "stop", "target", "exit_ts", "exit_px")
    assert done_full
    assert [tuple(r[k] for k in key) for r in done_part] == [
        tuple(r[k] for k in key) for r in done_full
    ]
