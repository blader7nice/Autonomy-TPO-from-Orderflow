"""ITERATION-5 (method5) unit tests: ATR(15m), T2 candles and window, the 50/50 split exit
(level and trail), bias invariants, no-lookahead truncation, and the window loader guard."""

from pathlib import Path

import numpy as np
import polars as pl

from flowlevels.config import load_config
from flowlevels.research import iter5
from flowlevels.research.trades import Path1m
from flowlevels.strategies import method4 as m4
from flowlevels.strategies import method5 as m5

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "configs/default.yaml")


def bars(o, h, lo, c, step=m4.M30, t0=0) -> m4.Bars:  # type: ignore[no-untyped-def]
    n = len(o)
    return m4.Bars(step, np.arange(n) * step + t0, *(np.asarray(x, float) for x in (o, h, lo, c)))


def test_atr_before_excludes_bar_itself() -> None:
    n = 20
    c = np.full(n, 100.0)
    h, lo = c + 1, c - 1  # true range 2 everywhere (bar 0 undefined)
    h[15] = 150.0  # huge range on bar 15 must not enter ATR[15]
    b = bars(c, h, lo, c, step=m4.M15)
    a = m5.atr_before(b, 14)
    assert np.isnan(a[14]) and a[15] == 2.0
    assert a[16] > 2.0  # bar 15 enters from bar 16 on


def test_confirm_t2_engulf_pin_and_far_edge() -> None:
    # bar 1 engulfs bar 0's body (long), close above the far edge
    b = bars([100.5, 100.0], [101, 102], [99.5, 99.8], [100.2, 101.5])
    assert m5.confirm_t2(b, 1, 1, far=99.0, wick_body=2, third=2 / 3) == "engulf"
    assert m5.confirm_t2(b, 1, 1, far=101.6, wick_body=2, third=2 / 3) == ""  # closes below far
    # long pin bar: body 0.2, lower wick 1.8, close in the top third
    p = bars([100.5, 100.0], [101, 100.3], [99, 98.2], [99.9, 100.2])
    assert m5.confirm_t2(p, 1, 1, far=98.0, wick_body=2, third=2 / 3) == "pin"
    # short pin bar mirrored
    s = bars([99.5, 100.0], [101, 101.8], [99, 99.7], [100.1, 99.8])
    assert m5.confirm_t2(s, 1, -1, far=102.0, wick_body=2, third=2 / 3) == "pin"
    assert m5.confirm_t2(s, 1, 1, far=90.0, wick_body=2, third=2 / 3) == ""


def test_t2_window_is_touch_bar_plus_two() -> None:
    # bars: 0 above, 1 touches 100 (near), 2 and 3 nothing, 4 a pin -> outside the window
    o = [102, 101, 100.8, 100.8, 100.8]
    h = [103, 101.2, 101, 101, 101.0]
    lo = [101.5, 99.9, 100.6, 100.6, 99.0]
    c = [101, 100.1, 100.8, 100.8, 100.9]
    b = bars(o, h, lo, c)
    p = m4.POI(1, "zone_30m", 100.0, 99.5, 0, 10 * m4.M30)
    meta: dict = {}
    assert m5.t2_setups([p], b, CFG, meta) == [] and meta["t2_touches"] == 1
    # the same pin one bar earlier (bar 3 = t0 + 2) confirms; entry at bar 4's open
    lo2 = lo.copy()
    lo2[3] = 99.0
    c2 = c.copy()
    c2[3] = 100.9
    b2 = bars(o, h, lo2, c2)
    p2 = m4.POI(1, "zone_30m", 100.0, 99.5, 0, 10 * m4.M30)
    out = m5.t2_setups([p2], b2, CFG, {})
    assert len(out) == 1 and out[0].dec_ts == 4 * m4.M30 and out[0].a_ts == m4.M30
    assert out[0].far == 99.5


def _path(highs: list[float], lows: list[float], opens: list[float] | None = None) -> Path1m:
    n = len(highs)
    op = np.asarray(opens if opens else [100.0] * n, float)
    hi, lo = np.asarray(highs, float), np.asarray(lows, float)
    return Path1m(np.arange(n) * 60_000, op, hi, lo, (hi + lo) / 2)


def test_split_lvl_tp1_then_tp2() -> None:
    p = _path([100.5, 102.1, 101, 104.2], [99.5, 100, 100.5, 101])
    f = m5.simulate_split(p, 0, 1, 98.0, 2.0, 102.0, 104.0, "lvl", 1.0, 100)
    assert (f.kind, f.x1_px, f.x2_px, f.x1_ts, f.x2_ts) == (
        "tp1+tp2",
        102.0,
        104.0,
        60_000,
        180_000,
    )


def test_split_stop_before_tp1_same_minute() -> None:
    p = _path([100.5, 102.5], [99.5, 97.0])
    f = m5.simulate_split(p, 0, 1, 98.0, 2.0, 102.0, 104.0, "lvl", 1.0, 100)
    assert (f.kind, f.x1_px, f.x2_px) == ("stop", 98.0, 98.0) and f.mae_r == 1.0


def test_split_lvl_tp1_then_stop_and_no_tp2() -> None:
    p = _path([102.0, 101, 100], [100, 99, 97.5])
    f = m5.simulate_split(p, 0, 1, 98.0, 2.0, 102.0, None, "lvl", 1.0, 100)
    assert (f.kind, f.x1_px, f.x2_px) == ("tp1+stop", 102.0, 98.0)


def test_split_trail_uses_completed_minutes_and_gap_open() -> None:
    # TP1 at minute 0 (high 103); trail from minute 1 = max(98, 103 - 1.5) = 101.5
    # minute 1: high 104 (trail for minute 2 = 102.5); minute 2 opens 102 < 102.5 -> fill at open
    p = _path([103, 104, 102.2], [100.5, 102.6, 101.0], opens=[100, 103, 102.0])
    f = m5.simulate_split(p, 0, 1, 98.0, 2.0, 102.0, 106.0, "trail", 1.5, 100)
    assert (f.kind, f.x1_px, f.x2_px, f.x2_ts) == ("tp1+trail", 102.0, 102.0, 120_000)
    # short mirror, no gap: fill at the trail
    q = _path([99.5, 98.0, 99.0], [97.0, 96.5, 97.5], opens=[100, 97.5, 97.6])
    g = m5.simulate_split(q, 0, -1, 102.0, 2.0, 98.0, 94.0, "trail", 1.5, 100)
    assert g.kind == "tp1+trail" and g.x2_px == 96.5 + 1.5


def test_split_time_exit() -> None:
    p = _path([100.5] * 5, [99.5] * 5)
    f = m5.simulate_split(p, 0, 1, 98.0, 2.0, 102.0, 104.0, "lvl", 1.0, 3)
    assert f.kind == "time" and f.x1_ts == f.x2_ts == 120_000 and f.x1_px == 100.0


def test_levels_ahead_sorted_nearest_first() -> None:
    lv = m4.Levels()
    lv.day[0] = m4.VA(110.0, 95.0, 104.0)
    out = m5.levels_ahead(1, 100.0, 3_600_000, lv, [])
    assert out == [104.0, 110.0]
    assert m5.levels_ahead(-1, 100.0, 3_600_000, lv, []) == [95.0]


def _random_klines(days: int, seed: int) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    n = days * 1440
    t0 = 1_704_067_200_000  # 2024-01-01
    drift = np.repeat(rng.normal(0, 6, days), 1440)  # trending days
    c = 90_000 + np.cumsum(rng.standard_t(4, n) * 15.0 + drift / 60)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.exponential(8, n)
    lo = np.minimum(o, c) - rng.exponential(8, n)
    return pl.DataFrame(
        {"minute_ts": t0 + np.arange(n) * 60_000, "open": o, "high": h, "low": lo, "close": c,
         "volume": rng.exponential(10, n)}
    )  # fmt: skip


def test_run_symbol_bias_invariants_and_no_lookahead() -> None:
    m = _random_klines(150, 11)
    t0 = int(m["minute_ts"][0])
    start, end = t0 + 110 * m4.DAY, t0 + 150 * m4.DAY
    i5 = CFG.iteration5.model_copy(update={"tp1_min_r": 0.3, "sweep_range_k_atr15": 0.5})
    cfg = CFG.model_copy(update={"iteration5": i5})
    full, meta = m5.run_symbol(m, start, end, cfg, "BTCUSDT")
    assert full, f"synthetic data should produce some trades: {meta['skips']}"
    assert {r["trigger"] for r in full} == {"T1", "T2"}
    for r in full:
        d = r["direction"]
        assert d * (r["entry"] - r["ema4"]) > 0 and d * (r["close_d"] - r["emad"]) > 0
        assert d * (r["tp1"] - r["entry"]) >= 0.3 * r["r_unit"] - 1e-9
        assert d * (r["entry"] - r["stop"]) >= 0.5 * r["atr1h"] - 1e-9
        assert start <= r["entry_ts"] < end
    cut = start + 20 * m4.DAY
    part, _ = m5.run_symbol(m.filter(pl.col("minute_ts") < cut), start, cut, cfg, "BTCUSDT")
    done_full = [r for r in full if r["exit_ts"] < cut - m4.MIN and r["entry_ts"] < cut]
    done_part = [r for r in part if not r["window_end_close"]]
    key = ("config", "entry_ts", "direction", "stop", "tp1", "tp2", "exit_ts", "exit_px")
    assert done_full
    as_t = [tuple(r[k] for k in key) for r in done_part]
    exp = [tuple(r[k] for k in key) for r in done_full]
    # tp2 may be nan: compare via repr
    assert [repr(x) for x in as_t] == [repr(x) for x in exp]


def test_window_loader_never_returns_holdout_minutes_for_a(tmp_path: Path) -> None:
    hs = iter5._ms(CFG.plan.holdout_start)
    ts = np.arange(hs - 3 * m4.DAY, hs + 3 * m4.DAY, 60_000)
    df = pl.DataFrame(
        {
            "minute_ts": ts,
            **{c: np.ones(len(ts)) for c in ("open", "high", "low", "close", "volume")},
        }
    )
    (tmp_path / "BTCUSDT").mkdir()
    df.write_parquet(tmp_path / "BTCUSDT" / "2025-12.parquet")
    a = iter5.load_window5(tmp_path, "BTCUSDT", CFG, "A")
    assert a.height and int(a["minute_ts"].max()) < hs
