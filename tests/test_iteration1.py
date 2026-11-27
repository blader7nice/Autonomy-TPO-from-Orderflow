"""ITERATION-1 rules: minimum stop, target zone and filter, selection and kill rule."""

from pathlib import Path

import numpy as np
import polars as pl
import pytest

from flowlevels.config import load_config
from flowlevels.levels.engine import Level, Zone
from flowlevels.research.backtest import SymbolData
from flowlevels.research.iteration1 import Trader, round_trip_cost, target_zone
from flowlevels.research.iteration1_report import grid_table, render
from flowlevels.research.touches import SessionZones
from flowlevels.research.trades import Path1m
from tests.helpers import T0

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "configs/default.yaml")
HOUR = 3_600_000


def _z(zid: str, lo: float, hi: float) -> Zone:
    return Zone(zid, zid, 2.0, (lo + hi) / 2, lo, hi, (Level("A", "A", (lo + hi) / 2),), "A")


ZONES = [_z("below", 99_000, 99_200), _z("touched", 99_900, 100_100), _z("above", 101_500, 101_700)]
SZ = SessionZones(T0, 500.0, ZONES)


def test_target_zone_is_nearest_other_zone_edge() -> None:
    assert target_zone(SZ, 100_000, 1, "touched") == 101_500
    assert target_zone(SZ, 100_000, -1, "touched") == 99_200
    assert target_zone(SZ, 101_600, 1, "touched") is None  # inside the last zone
    assert target_zone(SZ, 99_800, 1, "x") == 99_900  # the touched zone is excluded only by id


def test_round_trip_cost_default_scenario() -> None:
    # 2 x 0.05% x 100,000 + 2 x 1 bucket (10 USD)
    assert round_trip_cost(CFG, "BTCUSDT", 100_000) == pytest.approx(120.0)


def _trader(n_hours: int = 20) -> Trader:
    # 1m path: flat 100,000 with 300 USD hourly ranges -> ATR(1h) = 300
    n = n_hours * 60
    ts = T0 + 60_000 * np.arange(n)
    hour_lo = np.where((np.arange(n) % 60) == 0, 99_850.0, 100_000.0)
    hour_hi = np.where((np.arange(n) % 60) == 1, 100_150.0, 100_000.0)
    oh = pl.DataFrame(
        {"minute_ts": ts, "open": 100_000.0, "high": hour_hi, "low": hour_lo, "close": 100_000.0}
    )
    path = Path1m(ts, oh["open"].to_numpy(), hour_hi, hour_lo, oh["close"].to_numpy())
    sd = SymbolData(
        "BTCUSDT", oh, pl.DataFrame(), {T0: SZ}, pl.DataFrame(), path, pl.DataFrame(), []
    )
    bars = (
        oh.with_columns((pl.col("minute_ts") // 300_000 * 300_000).alias("bar_ts"))
        .group_by("bar_ts", maintain_order=True)
        .agg(pl.col("high").max(), pl.col("low").min())
    )
    return Trader(sd, CFG, bars)


def test_minimum_stop_and_target_filter() -> None:
    tr = _trader()
    p = 12 * 15  # a 5m bar in hour 15 (ATR known); bar range 99,850..100,150 or flat
    res = tr.trade(p, 1, 0.5, T0, "touched")
    assert isinstance(res, list)
    r0 = res[0]
    # min stop = max(0.5 x 300 = 150, 3 x 120 = 360) = 360 > bar-extreme distance
    assert r0["r_unit"] == pytest.approx(360.0) and r0["min_stop_bound"]
    assert r0["target"] == 101_500 and r0["target_r"] == pytest.approx(1500 / 360)
    assert [r["exit"] for r in res] == ["zone", "2R", "3R"]
    # k = 1.0 x ATR(300) -> stop 360 still (cost floor); short target 99,200 -> 800/360 >= 1.5
    assert isinstance(tr.trade(p, -1, 1.0, T0, "touched"), list)
    # with the touched zone as the only target candidate too close -> skipped
    tr2 = _trader()
    tr2.sd.sessions[T0] = SessionZones(T0, 500.0, [ZONES[1], _z("near", 100_300, 100_400)])
    assert tr2.trade(p, 1, 0.5, T0, "touched") == "target_too_close"
    assert tr2.trade(p, -1, 0.5, T0, "touched") == "no_target_zone"


def _grid_rows(exp: list[float], n: list[int]) -> pl.DataFrame:
    """Fake costed trades: one config per (thr, w, k) with given mean R and N."""
    rows = []
    it = CFG.iteration1
    cfgs = [
        (t, w, k)
        for t in it.grid_score_threshold
        for w in it.grid_window_bars
        for k in it.grid_min_stop_k_atr
    ]
    for (t, w, k), e, nn in zip(cfgs, exp, n, strict=True):
        for i in range(nn):
            for g in ("treatment", "unconfirmed"):
                px = 100_000.0 + 1000 * (e + (0.5 if i % 2 else -0.5))
                rows.append(
                    {
                        "group": g, "thr": t, "w": w, "k_atr": k, "exit": "zone",
                        "symbol": "BTCUSDT", "session": T0 + (i % 20) * 86_400_000,
                        "entry_ts": T0 + i, "direction": 1, "entry": 100_000.0,
                        "r_unit": 1000.0, "exit_px": px, "gross_r": e, "mfe_r": 1.0,
                        "mae_r": 0.5, "valid": True, "target_r": 2.0,
                        "min_stop_bound": True, "exit_kind": "target",
                    }
                )  # fmt: skip
    return pl.DataFrame(rows)


def test_selection_rule_and_kill() -> None:
    from flowlevels.research.report import apply_costs

    exp = [0.5, 3.0, 0.2, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1]
    n = [120, 50, 150] + [100] * 9  # config 2 is best but N < 100 -> not eligible
    g = grid_table(apply_costs(_grid_rows(exp, n), CFG), CFG)
    assert g.height == 12
    sel = g.filter(pl.col("selected"))
    assert sel.height == 1 and sel["n_t"][0] == 120  # highest exp among eligible
    meta = {"run_id": "t", "commit": "c", "config_hash": "h", "created_utc": "z",
            "days": ["a", "b"], "n_days": 1, "symbols": ["BTCUSDT"], "sessions": {},
            "min_zone_score_used": 2.0, "zone_score_median": 2.0, "setups": {},
            "skips": {}}  # fmt: skip
    md, out = render(_grid_rows(exp, n), meta, CFG)
    assert out["verdict"] == "POSITIVE" and "not killed" in md
    md, out = render(_grid_rows([-0.5] * 12, [120] * 12), meta, CFG)
    assert out["verdict"] == "KILL" and "KILLED" in md
    md, out = render(_grid_rows([1.0] * 12, [50] * 12), meta, CFG)
    assert out["verdict"] == "KILL" and "nothing can be selected" in md
