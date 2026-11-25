import math
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flowlevels.config import LevelsConfig, SymbolConfig, load_config
from flowlevels.footprint.minute import MinuteBuilder
from flowlevels.levels.engine import Level, atr_1h, compute_levels, confluence
from flowlevels.levels.profile import Profile, hvn_lvn, tpo_profile, value_area
from flowlevels.levels.vwap import VwapAccumulator, anchor_ms, anchored_vwap
from tests.helpers import T0, synthetic_trades

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "configs/default.yaml")
SC = CFG.symbol("BTCUSDT")
LC = CFG.levels
DAY = 86_400_000


def _prof(counts: list[float], start: int = 100) -> Profile:
    return Profile(np.arange(start, start + len(counts)), np.array(counts, float), 10.0)


# ------------------------------------------------------------------ value area / POC
def test_value_area_dual_row_expansion() -> None:
    # rows 100..108, total 40, target 28
    p = _prof([1, 2, 3, 6, 9, 8, 5, 4, 2])
    # start POC row 104 (9). up pair (8+5=13) > down pair (6+3=9) -> add up: 22, rows 104-106
    # up (4+2=6) vs down (9) -> add down: 31 >= 28 -> stop. VA rows 102..106
    va = value_area(p, 0.70)
    assert va is not None
    assert va.poc_row == 104 and va.poc == 1045.0
    assert va.val == 1020.0 and va.vah == 1070.0


def test_value_area_tie_adds_both_sides() -> None:
    p = _prof([1, 3, 3, 10, 3, 3, 1])  # total 24, target 16.8; up=6, down=6 -> add both
    va = value_area(p, 0.70)
    assert va is not None and va.val == 1010.0 and va.vah == 1060.0


def test_value_area_single_row_at_edge() -> None:
    p = _prof([10, 1, 1])
    va = value_area(p, 0.70)
    assert va is not None and va.poc_row == 100 and va.val == 1000.0
    assert value_area(_prof([]), 0.7) is None


def test_poc_tie_break_closest_to_middle_then_lower() -> None:
    assert value_area(_prof([5, 1, 5, 1, 5]), 0.1).poc_row == 102  # type: ignore[union-attr]
    assert value_area(_prof([5, 1, 1, 5]), 0.1).poc_row == 100  # type: ignore[union-attr]


def test_tpo_profile_counts_periods() -> None:
    # Two 30-min periods: P1 range 1000..1029.9, P2 range 1020..1040
    oh = pl.DataFrame(
        {
            "minute_ts": [T0, T0 + 60_000, T0 + 1_800_000],
            "high": [1015.0, 1029.9, 1040.0],
            "low": [1000.0, 1010.0, 1020.0],
        }
    )
    p = tpo_profile(oh, 30, 10.0, 0.1)
    assert p.rows.tolist() == [100, 101, 102, 103, 104]
    assert p.counts.tolist() == [1, 1, 2, 1, 1]


def test_hvn_lvn_bimodal() -> None:
    counts = [1, 2, 8, 9, 8, 2, 1, 1, 2, 7, 10, 7, 2, 1]
    hv, lv = hvn_lvn(_prof(counts), window=1, prominence=0.1)
    assert hv == [1035.0, 1105.0]
    assert lv == [1065.0]  # first minimum between the peaks (rows 106, 107 tie -> 106)


# ------------------------------------------------------------------ VWAP
def test_vwap_and_sigma_hand_computed() -> None:
    # trades: 2 @ 100, 1 @ 103 -> vwap 101, var = (2*1 + 1*4)/3 = 2
    oh = pl.DataFrame(
        {
            "minute_ts": [T0],
            "volume": [3.0],
            "sum_pv": [2 * 100 + 103.0],
            "sum_p2v": [2 * 100**2 + 103.0**2],
        }
    )
    acc = VwapAccumulator(T0)
    acc.add_rows(oh)
    assert acc.vwap == pytest.approx(101.0)
    assert acc.sigma == pytest.approx(math.sqrt(2))
    assert acc.bands()["+2s"] == pytest.approx(101 + 2 * math.sqrt(2))


def test_anchors() -> None:
    t = 1_767_571_200_000 + 13 * 3_600_000  # 2026-01-05 (Monday) 13:00 UTC
    assert anchor_ms("d", t) == 1_767_571_200_000
    assert anchor_ms("w", t) == 1_767_571_200_000
    assert anchor_ms("m", t) == T0  # 2026-01-01


# ------------------------------------------------------------------ confluence
def test_confluence_clusters_and_scores() -> None:
    lc = LevelsConfig(confluence_k_atr=0.5, zone_tolerance_k_atr=0.1, weights={"nPOC": 2.0})
    sc = SymbolConfig(exchange_tick=0.1, bucket=10, tpo_tick=10)
    lv = [
        Level("VAL d", "VAL d", 100.0),
        Level("nPOC x", "nPOC", 104.0),
        Level("PDH", "PDH", 120.0),
        Level("VWAP d", "VWAP d", 108.0),
    ]
    zones = confluence("BTCUSDT", lv, atr=10.0, sc=sc, lc=lc)
    # gap 5: 100,104,108 chain; 120 separate
    assert [z.label for z in zones] == ["VAL d | nPOC x | VWAP d", "PDH"]
    z = zones[0]
    assert z.score == 4.0
    assert z.price == pytest.approx((100 + 2 * 104 + 108) / 4)
    assert (z.low, z.high) == (99.0, 109.0)


# ------------------------------------------------------------------ engine + no lookahead
TAPE = synthetic_trades(40_000, seed=11, start_ms=T0, span_ms=12 * DAY, tick=0.1)


def _build(trades: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    b = MinuteBuilder(SC.exchange_tick, SC.bucket)
    a = b.push(trades)
    z = b.flush()
    return pl.concat([a.ohlcv, z.ohlcv]), pl.concat([a.footprint, z.footprint])


OH_FULL, FP_FULL = _build(TAPE)


def test_engine_produces_expected_level_types() -> None:
    t = T0 + 10 * DAY + 5 * 3_600_000
    ls = compute_levels("BTCUSDT", OH_FULL, FP_FULL, t, SC, LC)
    names = {lv.name for lv in ls.levels}
    for n in (
        "POC d",
        "VAH d",
        "VAL d",
        "vPOC d",
        "vPOC w",
        "PDH",
        "PWL",
        "dPOC",
        "VWAP d",
        "VWAP w +1s",
        "VWAP m -2s",
    ):
        assert n in names, n
    assert ls.atr_1h and ls.atr_1h > 0 and ls.zones
    assert ls.level("VAL d") <= ls.level("POC d") <= ls.level("VAH d")  # type: ignore[operator]
    prior = OH_FULL.filter(
        (pl.col("minute_ts") >= T0 + 9 * DAY) & (pl.col("minute_ts") < T0 + 10 * DAY)
    )
    assert ls.level("PDH") == prior["high"].max()
    vw = anchored_vwap(OH_FULL.filter(pl.col("minute_ts") + 60_000 <= t), "d", t)
    assert ls.level("VWAP d") == pytest.approx(vw.vwap)


def _levels_dict(ls: object) -> dict[str, float]:
    return {lv.name: lv.price for lv in ls.levels}  # type: ignore[attr-defined]


@settings(max_examples=25, deadline=None)
@given(st.integers(min_value=T0 + 2 * DAY, max_value=T0 + 12 * DAY - 1))
def test_no_lookahead_levels(t: int) -> None:
    """Levels at t from the FULL dataset == levels from trades truncated at t (ts < t).

    The truncated rebuild contains a partial open minute; the engine must ignore it.
    """
    oh_t, fp_t = _build(TAPE.filter(pl.col("ts_ms") < t))
    full = compute_levels("BTCUSDT", OH_FULL, FP_FULL, t, SC, LC)
    trunc = compute_levels("BTCUSDT", oh_t, fp_t, t, SC, LC)
    a, b = _levels_dict(full), _levels_dict(trunc)
    assert a.keys() == b.keys()
    for k in a:
        assert a[k] == pytest.approx(b[k], rel=1e-12), k
    assert full.atr_1h == trunc.atr_1h
    assert [z.zone_id for z in full.zones] == [z.zone_id for z in trunc.zones]


def test_atr_needs_enough_history() -> None:
    assert atr_1h(OH_FULL, T0 + 3_600_000, 14) is None
    assert atr_1h(OH_FULL, T0 + DAY, 14) is not None
