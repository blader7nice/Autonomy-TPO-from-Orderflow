"""Regression on REAL data: BTCUSDT 2026-11-17 (the day that showed the v1 zone chaining).

Fixture (tests/fixtures/README.md): real Binance USD-M BTCUSDT 1m OHLCV 2026-11-01 … 11-17
and the footprint summed per hour, which gives identical levels at hourly snapshots (checked
when the fixture was built). With v1 (height-based HVN/LVN, single-linkage) this day had
53 HVN+LVN and a 1,750 USD (3.9 ATR) zone at 12:00.
"""

from pathlib import Path

import polars as pl
import pytest

from flowlevels.config import load_config
from flowlevels.levels.engine import HOUR_MS, compute_levels

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests/fixtures"
CFG = load_config(ROOT / "configs/default.yaml")
D0 = 1_794_873_600_000  # 2026-11-17 00:00 UTC


@pytest.fixture(scope="module")
def snapshots() -> list:  # type: ignore[type-arg]
    oh = pl.read_parquet(FIX / "btcusdt-2026-11-01_17-ohlcv_1m.parquet")
    fp = pl.read_parquet(FIX / "btcusdt-2026-11-01_17-footprint_1h.parquet")
    sc, lc = CFG.symbol("BTCUSDT"), CFG.levels
    return [compute_levels("BTCUSDT", oh, fp, D0 + h * HOUR_MS, sc, lc) for h in range(24)]


def test_fixture_is_the_right_day() -> None:
    import datetime as dt

    assert dt.datetime.fromtimestamp(D0 / 1000, tz=dt.UTC).isoformat() == (
        "2026-11-17T00:00:00+00:00"
    )


def test_no_zone_exceeds_max_width(snapshots: list) -> None:  # type: ignore[type-arg]
    k = CFG.levels.zone_max_width_k_atr
    for ls in snapshots:
        assert ls.atr_1h
        for z in ls.zones:
            assert z.high - z.low <= k * ls.atr_1h + 1e-6, (ls.t_ms, z.label)


def test_node_counts_capped(snapshots: list) -> None:  # type: ignore[type-arg]
    lc = CFG.levels
    for ls in snapshots:
        hvn = [lv.price for lv in ls.levels if lv.type == "HVN d"]
        lvn = [lv.price for lv in ls.levels if lv.type == "LVN d"]
        assert len(hvn) <= lc.hvn_top_n and len(lvn) <= lc.lvn_top_n
        # nodes are constant within the session (ATR as of the session open)
        assert hvn == [lv.price for lv in snapshots[0].levels if lv.type == "HVN d"]


def test_zones_per_day_in_sane_range(snapshots: list) -> None:  # type: ignore[type-arg]
    # Measured with config v2 on this day: 33 levels, 23.8 zones and 7.7 confluence zones
    # (>= 2 members) per hourly snapshot (docs/sanity/levels-stats-2026-11-26.md). The bounds
    # are a sanity range, not a target: v1 had 80 levels and 1-3 chained giant zones.
    n_levels = len(snapshots[0].levels)
    conf = [sum(len(z.members) > 1 for z in ls.zones) for ls in snapshots]
    mean_conf = sum(conf) / len(conf)
    assert 15 <= n_levels <= 45
    assert 3 <= mean_conf <= 15
    assert max(conf) <= 20
