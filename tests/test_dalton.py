"""ITERATION-3 Dalton rules H1/H2: signal logic, weekly VWAP, grid size, hold-out guard."""

from pathlib import Path

import numpy as np
import polars as pl
import pytest

from flowlevels.config import load_config
from flowlevels.research import plan
from flowlevels.strategies import dalton as dl

CFG = load_config(Path(__file__).resolve().parents[1] / "configs/default.yaml")
D0 = 1_767_225_600_000
STEP = 30 * 60_000


def _sess(open0: float, closes: list[float], highs: list[float] | None = None,
          lows: list[float] | None = None) -> dl.Session:  # fmt: skip
    n = len(closes)
    c = np.array(closes)
    h = np.array(highs) if highs else c + 1
    lo = np.array(lows) if lows else c - 1
    return dl.Session(D0, 110.0, 90.0, 100.0, open0, D0 + STEP * (1 + np.arange(n)), h, lo, c)


def test_h1_two_consecutive_closes_inside() -> None:
    s = _sess(115.0, [112, 108, 111, 105, 104, 100])  # inside at p=1, 3, 4 -> first pair (3, 4)
    (x,) = dl.h1_signals(s)
    assert x.direction == -1 and x.decision_ts == D0 + 5 * STEP and x.target == 90.0
    assert x.raw_stop == 110.0  # re-entered edge; k x ATR / 3 RT added at entry
    assert dl.h1_signals(_sess(100.0, [100, 100, 100])) == []  # opened inside
    assert dl.h1_signals(_sess(85.0, [95, 96])) == []  # the pair ends on the last period
    (y,) = dl.h1_signals(_sess(85.0, [95, 96, 97]))
    assert y.direction == 1 and y.target == 110.0


def test_h2_first_probe_rejection_and_acceptance() -> None:
    # VAH probe at p=1 closing inside -> short; VAL probe at p=3 closing outside -> no long
    s = _sess(100.0, [100, 108, 100, 88, 95], highs=[101, 112, 101, 92, 96],
              lows=[99, 105, 99, 86, 94])  # fmt: skip
    (x,) = dl.h2_signals(s, 0.2)
    assert x.direction == -1 and x.edge == "VAH" and x.raw_stop == pytest.approx(112.2)
    assert x.decision_ts == D0 + 2 * STEP and x.target == 90.0
    # the first VAH probe is accepted (closes above) -> no later VAH trade
    s2 = _sess(100.0, [112, 100, 105, 100], highs=[113, 101, 111, 101], lows=[99, 99, 104, 99])
    assert dl.h2_signals(s2, 0.2) == []
    # an outside bar probing both edges and closing inside -> both skipped
    s3 = _sess(100.0, [100, 100], highs=[111, 101], lows=[89, 99])
    assert dl.h2_signals(s3, 0.2) == []
    assert dl.h2_signals(_sess(120.0, [100, 100]), 0.2) == []  # opened outside


def test_weekly_vwap_exclusive_and_resets_monday() -> None:
    mon = 1_767_571_200_000  # 2026-01-05 00:00 UTC (Monday)
    ts = np.r_[mon - 2 * 60_000, mon - 60_000, mon, mon + 60_000, mon + 120_000]
    oh = pl.DataFrame({"minute_ts": ts, "high": [10.0, 20, 30, 40, 50], "low": [10.0, 20, 30, 40, 50],
                       "close": [10.0, 20, 30, 40, 50], "volume": [1.0, 1, 1, 3, 1]})  # fmt: skip
    _, vw = dl.weekly_vwap_before(oh)
    assert vw[1] == 10.0 and np.isnan(vw[2]) and vw[3] == 30.0
    assert vw[4] == pytest.approx((30 + 120) / 4)


def test_grid_sizes() -> None:
    g = dl.grids()
    assert sum(x.rule == "H1" for x in g) == 4 and sum(x.rule == "H2" for x in g) == 8
    assert len({x.key for x in g}) == 12


def test_windows_do_not_touch_holdout() -> None:
    w = plan.windows(CFG)
    hs = plan._ms(CFG.plan.holdout_start)
    he = plan._ms(CFG.plan.holdout_end) + plan.DAY_MS
    assert w["A"][1] == hs and w["B"][0] == he
    assert (w["A"][1] - w["A"][0]) // plan.DAY_MS == 365
    assert (w["B"][1] - w["B"][0]) // plan.DAY_MS == 60
