import numpy as np
import polars as pl
import pytest

from flowlevels.footprint.bars import (
    Ladder,
    aggregate_footprint,
    aggregate_ohlcv,
    imbalances,
    ladder,
    stacked_runs,
    summarize_bar,
)
from flowlevels.footprint.minute import footprint_from_trades, ohlcv_from_trades
from tests.helpers import T0, synthetic_trades

TAPE = synthetic_trades(4_000, span_ms=2 * 3_600_000)
FP1 = footprint_from_trades(TAPE, 0.1, 10.0)
OH1 = ohlcv_from_trades(TAPE)


@pytest.mark.parametrize(("res", "step"), [("3m", 180_000), ("5m", 300_000), ("1h", 3_600_000)])
def test_aggregation_equals_direct_computation(res: str, step: int) -> None:
    agg = aggregate_footprint(FP1, res)
    # Direct: treat each `res` bar as one "minute" by flooring trade times to the bar.
    shifted = TAPE.with_columns((pl.col("ts_ms") // step * step).alias("ts_ms"))
    direct = footprint_from_trades(shifted, 0.1, 10.0)
    # footprint_from_trades floors to minutes; bar starts are minute multiples, so equal.
    direct = direct.rename({"minute_ts": "bar_ts"})
    cols = [
        "bar_ts",
        "bucket",
        "ask_vol",
        "bid_vol",
        "ask_trades",
        "bid_trades",
        "ask_max_trade",
        "bid_max_trade",
    ]
    a, d = agg.select(cols).sort("bar_ts", "bucket"), direct.select(cols).sort("bar_ts", "bucket")
    assert a.select("bar_ts", "bucket", "ask_trades", "bid_trades").equals(
        d.select("bar_ts", "bucket", "ask_trades", "bid_trades")
    )
    for c in ("ask_vol", "bid_vol", "ask_max_trade", "bid_max_trade"):
        assert np.allclose(a[c].to_numpy(), d[c].to_numpy())
    oh = aggregate_ohlcv(OH1, res)
    oh_direct = ohlcv_from_trades(shifted)
    assert oh["open"].to_list() == oh_direct["open"].to_list()
    assert oh["high"].to_list() == oh_direct["high"].to_list()
    assert oh["close"].to_list() == oh_direct["close"].to_list()
    assert np.allclose(oh["volume"].to_numpy(), oh_direct["volume"].to_numpy())


def test_delta_and_cvd_with_session_reset() -> None:
    day = 86_400_000
    oh = pl.DataFrame(
        {
            "minute_ts": [T0 + day - 120_000, T0 + day - 60_000, T0 + day, T0 + day + 60_000],
            "open": [1.0] * 4,
            "high": [1.0] * 4,
            "low": [1.0] * 4,
            "close": [1.0] * 4,
            "volume": [5.0, 3.0, 4.0, 2.0],
            "buy_volume": [4.0, 1.0, 3.0, 0.0],
            "sell_volume": [1.0, 2.0, 1.0, 2.0],
            "n_trades": [1] * 4,
            "sum_pv": [0.0] * 4,
            "sum_p2v": [0.0] * 4,
            "first_id": [1, 2, 3, 4],
            "last_id": [1, 2, 3, 4],
        }
    )
    b = aggregate_ohlcv(oh, "1m")
    assert b["delta"].to_list() == [3.0, -1.0, 2.0, -2.0]
    assert b["cvd"].to_list() == [3.0, 2.0, 4.0, 2.0]
    assert b["cvd_session"].to_list() == [3.0, 2.0, 2.0, 0.0]


def _lad(ask: list[float], bid: list[float]) -> Ladder:
    n = len(ask)
    return Ladder(
        0,
        np.arange(100, 100 + n),
        np.array(ask, float),
        np.array(bid, float),
        np.ones(n, np.int64),
        np.ones(n, np.int64),
    )


def test_diagonal_imbalances_hand_computed() -> None:
    # bucket:      100  101  102  103  104
    ask = [1.0, 9.0, 3.0, 0.0, 10.0]
    bid = [3.0, 2.0, 1.0, 5.0, 1.0]
    buy, sell = imbalances(_lad(ask, bid), ratio=3.0, min_volume=0.0)
    # buy at p: ask[p] >= 3*bid[p-1]: 101: 9>=9 T; 102: 3>=6 F; 103: ask 0 F; 104: 10>=15 F
    assert buy.tolist() == [False, True, False, False, False]
    # sell at p: bid[p] >= 3*ask[p+1]: 100: 3>=27 F; 101: 2>=9 F; 102: 1>=0 T; 103: 5>=30 F
    assert sell.tolist() == [False, False, True, False, False]
    # minimum volume filter
    buy2, sell2 = imbalances(_lad(ask, bid), ratio=3.0, min_volume=2.0)
    assert buy2.tolist() == [False, True, False, False, False]
    assert sell2.tolist() == [False] * 5


def test_stacked_runs() -> None:
    f = np.array([1, 1, 1, 0, 1, 1, 0, 1, 1, 1, 1], dtype=bool)
    assert stacked_runs(f, 3) == [(0, 2), (7, 10)]
    assert stacked_runs(f, 2) == [(0, 2), (4, 5), (7, 10)]


def test_stacked_buy_imbalance_in_summary() -> None:
    ask = [0.0, 10.0, 10.0, 10.0, 1.0]
    bid = [1.0, 1.0, 1.0, 1.0, 1.0]
    s = summarize_bar(_lad(ask, bid), 3.0, 0.0, 3)
    assert s.buy_imb_buckets == [101, 102, 103]
    assert s.stacked_buy == [(101, 103)]
    assert s.delta == pytest.approx(31 - 5)


def test_bar_poc_tie_break() -> None:
    # totals: 5, 7, 7, 2, 7 -> candidates 101, 102, 104; mid index 2 -> 102
    assert _lad([5, 7, 7, 2, 7], [0, 0, 0, 0, 0]).poc_bucket() == 102
    # totals: 7, 1, 1, 7 -> mid 1.5, distance tie -> lower bucket
    assert _lad([7, 1, 1, 7], [0, 0, 0, 0]).poc_bucket() == 100


def test_ladder_fills_untraded_buckets() -> None:
    rows = pl.DataFrame(
        {
            "bucket": [10, 13],
            "ask_vol": [1.0, 2.0],
            "bid_vol": [0.5, 0.0],
            "ask_trades": [1, 1],
            "bid_trades": [1, 0],
        }
    )
    lad = ladder(rows, 0)
    assert lad.buckets.tolist() == [10, 11, 12, 13]
    assert lad.ask.tolist() == [1.0, 0.0, 0.0, 2.0]
