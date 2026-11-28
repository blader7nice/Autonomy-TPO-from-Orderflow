"""Supply Zone port. Ported from the owner's test_supply_zone_bot.py where the test concerns
strategy logic (regime, TP1 -> break-even + trailing, scan path with TP1/TP2 and the RR gate);
exchange/credential/Telegram/state-file/account-limit tests have no counterpart (spec A6)."""

from pathlib import Path

import numpy as np
import pytest

from flowlevels.config import SupplyZoneConfig, load_config
from flowlevels.strategies import supply_zone as sz

ROOT = Path(__file__).resolve().parents[1]
P = SupplyZoneConfig()
T0 = 1_767_225_600_000  # 2026-01-01 00:00 UTC
H = sz.HOUR_MS


def make_ohlcv(rows: int, base: float, step: float, step_ms: int = H) -> sz.Bars:
    """The original make_ohlcv fixture."""
    close = base + step * np.arange(rows)
    return sz.Bars(
        T0 + step_ms * np.arange(rows), close - 0.2, close + 0.5, close - 0.5, close,
        np.full(rows, 1000.0),
    )  # fmt: skip


def test_config_matches_live_defaults() -> None:
    cfg = load_config(ROOT / "configs/default.yaml")
    assert cfg.supply_zone.model_dump(exclude={"regime_thresholds"}) == P.model_dump(
        exclude={"regime_thresholds"}
    )
    assert cfg.supply_zone.threshold("BTC") == 0.05 and cfg.supply_zone.threshold("DOGE") == 0.1


# ---- ported: test_engine_detects_bull_and_bear_regimes
def test_regime_bull_and_bear() -> None:
    # "BTC/USDT:USDT" is not in the bot's threshold dict -> code default 0.1
    assert sz.regime(make_ohlcv(140, 100.0, 1.0), 0.1, P) == "BULL"
    assert sz.regime(make_ohlcv(140, 200.0, -1.0), 0.1, P) == "BEAR"
    assert sz.regime(make_ohlcv(140, 100.0, 0.0), 0.1, P) == "CONSOLIDATION"


# ---- ported: test_engine_scan_only_returns_signal_with_professional_sl_and_partial_tp
def test_engine_levels_short_partial_tp_and_rr_gate() -> None:
    n = 30
    f4 = sz.Bars(
        T0 + 4 * H * np.arange(n), np.full(n, 99.0), np.full(n, 99.5), np.full(n, 98.5),
        np.full(n, 99.0), np.full(n, 1.0),
    )  # fmt: skip
    zone = sz.SZone(101.0, 96.0, 0, T0)
    reason, sl, tp1, tp2, atr = sz.engine_levels("short", 100.0, zone, f4, P)
    assert atr == pytest.approx(1.0)
    assert sl == pytest.approx(max(101 + 0.3, 99.5 + 0.2))
    assert tp1 == pytest.approx(98.5) and tp2 == pytest.approx(94.5)
    assert reason == "signal"  # RR = 5.5 / 1.3 >= 3
    reason, *_ = sz.engine_levels("short", 100.0, sz.SZone(101.0, 99.0, 0, T0), f4, P)
    assert reason in ("filter_rr_too_low", "insufficient_rr")  # TP2 97.5 -> RR 1.9
    # long mirror
    reason, sl, tp1, tp2, _ = sz.engine_levels("long", 100.0, sz.SZone(104.0, 99.0, 0, T0), f4, P)
    assert sl == pytest.approx(min(99 - 0.3, 98.5 - 0.2)) and tp1 == pytest.approx(101.5)
    assert tp2 == pytest.approx(105.5)


def test_indicators_match_pandas_semantics() -> None:
    x = np.array([1.0, 2.0, 3.0, 2.0, 4.0])
    e = sz.ema(x, 3)  # pandas ewm(span=3, adjust=False)
    assert e[0] == 1.0 and e[1] == pytest.approx(1.5) and e[2] == pytest.approx(2.25)
    m = sz.rolling_mean(x, 3)
    assert np.isnan(m[:2]).all() and m[2] == pytest.approx(2.0)
    r = sz.rsi_sma(np.array([1.0, 2.0, 3.0, 4.0]), 2)
    assert np.isnan(r[0]) and r[3] == 100.0  # no losses -> RSI 100
    b = sz.Bars(np.arange(3), np.ones(3), np.array([2.0, 5.0, 3.0]), np.array([1.0, 4.0, 1.0]),
                np.array([1.5, 4.5, 2.0]), np.ones(3))  # fmt: skip
    assert list(sz.true_range(b)) == [1.0, 3.5, 3.5]


def test_zones_vp_fvg() -> None:
    n = 40
    c = np.full(n, 100.0)
    o, hi, lo = c.copy(), c + 1, c - 1
    o[20], c[20], hi[20], lo[20] = 110.0, 100.0, 110.5, 99.5  # strong bearish -> supply
    c[25], hi[25] = 99.0, 100.5  # touches the zone bottom and closes below -> rejection
    b4 = sz.Bars(T0 + 4 * H * np.arange(n), o, hi, lo, c, np.ones(n))
    zs = sz.find_zones(b4, "supply", P)
    assert len(zs) == 1 and (zs[0].top, zs[0].bottom, zs[0].strength) == (110.0, 100.0, 1)
    assert sz.price_in_zone(110.4, zs, 0.5) is zs[0]
    assert sz.price_in_zone(111.0, zs, 0.5) is None
    assert sz.find_zones(b4, "demand", P) == []
    vb = sz.Bars(np.arange(3), np.ones(3), np.array([10.0, 10.0, 12.0]),
                 np.array([0.0, 8.0, 11.0]), np.ones(3), np.array([1.0, 50.0, 1.0]))  # fmt: skip
    assert 8.0 <= sz.vp_poc(vb, 50) <= 10.0
    fb = sz.Bars(np.arange(4), np.ones(4), np.array([100.0, 99.0, 97.0, 96.0]),
                 np.array([99.0, 97.0, 95.0, 94.0]), np.array([99.5, 98.0, 96.5, 98.5]),
                 np.ones(4))  # fmt: skip
    f = sz.active_fvgs(fb, P)  # low[0]=99 > high[2]=97 -> bearish (97, 99), close 98.5 -> 75 %
    assert [(x.kind, x.bottom, x.top) for x in f][:1] == [("bearish", 97.0, 99.0)]
    assert sz.price_in_fvg(98.0, f, "bearish") and not sz.price_in_fvg(98.0, f, "bullish")


def test_recalc_index_is_251_for_live_frames() -> None:
    f1 = make_ohlcv(300, 100.0, 0.0)
    f4 = make_ohlcv(150, 100.0, 0.0, 4 * H).sl(None, None)
    f4 = sz.Bars(f4.ts - 400 * H, f4.o, f4.h, f4.lo, f4.c, f4.v)
    assert sz.recalc_index(f1, f4, 50) == 251


def test_frames_forming_bar_and_no_lookahead() -> None:
    n = 60 * 30
    ts = T0 + 60_000 * np.arange(n)
    rng = np.random.default_rng(1)
    c = 100 + np.cumsum(rng.normal(0, 0.1, n))
    m1 = sz.Bars(ts, c, c + 0.05, c - 0.05, c, np.ones(n))
    fr = sz.Frames(m1)
    s = T0 + 20 * H + 3 * 60_000
    f1, f4 = fr.at(s, 10, 3)
    assert len(f1) == 10 and f1.ts[-1] == T0 + 20 * H
    assert f1.c[-1] == c[20 * 60 + 2] and f1.v[-1] == 3.0  # minutes 00..02 only
    assert f4.ts[-1] == T0 + 20 * H and f4.v[-1] == 3.0
    c2 = c.copy()
    c2[20 * 60 + 3 :] += 50  # the future must not matter
    fr2 = sz.Frames(sz.Bars(ts, c2, c2 + 0.05, c2 - 0.05, c2, np.ones(n)))
    g1, g4 = fr2.at(s, 10, 3)
    for a, b in ((f1, g1), (f4, g4)):
        for x, y in zip((a.o, a.h, a.lo, a.c, a.v), (b.o, b.h, b.lo, b.c, b.v), strict=True):
            assert np.array_equal(x, y)


def _path(prices: list[float], start: int) -> sz.Frames:
    """1m path, flat history before `start` so the frames exist."""
    pre = 400 * 60
    n = pre + len(prices)
    c = np.r_[np.full(pre, prices[0]), np.array(prices)]
    ts = start - pre * 60_000 + 60_000 * np.arange(n)
    return sz.Frames(sz.Bars(ts, c, c, c, c, np.ones(n)))


def _scan(s: int, entry: float, d: str) -> sz.Scan:
    core = sz.Core(d, entry, 0.0, 0.0, sz.SZone(101.0, 99.0, 0, 0), 1, 100.0)
    if d == "long":
        return sz.Scan(s, "BEAR", d, "signal", core, 95.0, 102.0, 110.0)
    return sz.Scan(s, "BULL", d, "signal", core, 105.0, 98.0, 90.0)


# ---- ported: test_position_tracker_adds_position_and_marks_tp1_hit (TP1 -> BE + trailing)
def test_tp1_then_break_even_at_next_hourly_tick() -> None:
    s = T0 + 500 * H + 3 * 60_000
    # TP1 (102) at minute 5; back to 100 at minute 30, before the next tick (HH+1:03)
    px = [100.0] * 5 + [102.5] + [101.0] * 24 + [99.0] * 10 + [94.0]
    tr = sz.manage(_path(px, s), _scan(s, 100.0, "long"), "BTCUSDT", s + 10 * H, P)
    assert tr is not None and tr.kind1 == "tp1" and tr.x1 == 102.5  # gapped open above TP1
    assert tr.kind2 == "stop" and tr.x2 == 94.0  # original stop still active, gap fill
    # same, but the drop comes after the tick: the stop is at the entry (break-even)
    px = [100.0] * 5 + [102.5] + [101.0] * 70 + [99.0]
    tr = sz.manage(_path(px, s), _scan(s, 100.0, "long"), "BTCUSDT", s + 10 * H, P)
    # stop moved to >= entry at the tick (BE, then the chandelier tightens it further on this
    # low-ATR path), so the remainder exits at the trailed stop, not at the original 95
    assert tr is not None and tr.kind2 == "trail" and tr.x2 >= 100.0
    assert tr.exit_px == pytest.approx(0.33 * 102.5 + 0.67 * tr.x2)
    assert tr.r_unit == pytest.approx(5.0)


def test_stop_first_tp2_and_window_end() -> None:
    s = T0 + 500 * H + 3 * 60_000
    tr = sz.manage(_path([100.0, 90.0], s), _scan(s, 100.0, "short"), "X", s + H, P)
    assert tr is not None and (tr.kind1, tr.kind2) == ("tp1", "tp2")  # both in one minute
    tr = sz.manage(
        _path([100.0, 100.0, 100.0], s), _scan(s, 100.0, "short"), "X", s + 2 * 60_000, P
    )
    assert tr is not None and tr.window_end_close and tr.kind2 == "window_end"
    # fill-price shift: signal entry 99 vs fill 100 -> levels shifted by +1
    sc = _scan(s, 99.0, "long")
    tr = sz.manage(_path([100.0, 100.0], s), sc, "X", s + 2 * 60_000, P)
    assert tr is not None and tr.sl == 96.0 and tr.tp1 == 103.0 and tr.tp2 == 111.0
