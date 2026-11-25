import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flowlevels.footprint.minute import (
    MinuteBuilder,
    OutOfOrderTradeError,
    footprint_from_trades,
    ohlcv_from_trades,
)
from flowlevels.stream.types import TRADE_SCHEMA
from tests.helpers import T0, synthetic_trades


def _tape(rows: list[tuple[int, int, float, float, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {"ts_ms": t, "trade_id": i, "price": p, "qty": q, "aggressor": a, "fill_count": 1}
            for (t, i, p, q, a) in rows
        ],
        schema=TRADE_SCHEMA,
    )


HAND = _tape(
    [
        # minute 0
        (T0 + 1_000, 1, 100_005.0, 1.0, "B"),  # bucket 10000 (100000..100010)
        (T0 + 2_000, 2, 100_009.9, 2.0, "S"),  # bucket 10000
        (T0 + 3_000, 3, 100_010.0, 0.5, "B"),  # bucket 10001
        (T0 + 59_999, 4, 100_000.0, 3.0, "B"),  # bucket 10000
        # minute 1
        (T0 + 60_000, 5, 99_999.9, 1.5, "S"),  # bucket 9999
        (T0 + 61_000, 6, 100_020.0, 0.25, "B"),  # bucket 10002
    ]
)


def test_footprint_hand_computed() -> None:
    fp = footprint_from_trades(HAND, exchange_tick=0.1, bucket_size=10.0)
    rows = {(r["minute_ts"], r["bucket"]): r for r in fp.iter_rows(named=True)}
    r = rows[(T0, 10000)]
    assert (r["ask_vol"], r["bid_vol"], r["ask_trades"], r["bid_trades"]) == (4.0, 2.0, 2, 1)
    assert (r["ask_max_trade"], r["bid_max_trade"]) == (3.0, 2.0)
    r = rows[(T0, 10001)]
    assert (r["ask_vol"], r["bid_vol"], r["bid_max_trade"]) == (0.5, 0.0, 0.0)
    assert rows[(T0 + 60_000, 9999)]["bid_vol"] == 1.5
    assert rows[(T0 + 60_000, 10002)]["ask_vol"] == 0.25
    assert fp.height == 4


def test_ohlcv_hand_computed() -> None:
    o = ohlcv_from_trades(HAND).row(0, named=True)
    assert (o["open"], o["high"], o["low"], o["close"]) == (
        100_005.0,
        100_010.0,
        100_000.0,
        100_000.0,
    )
    assert (o["volume"], o["buy_volume"], o["sell_volume"]) == (6.5, 4.5, 2.0)
    assert (o["n_trades"], o["first_id"], o["last_id"]) == (4, 1, 4)
    assert o["sum_pv"] == pytest.approx(100_005 * 1 + 100_009.9 * 2 + 100_010 * 0.5 + 100_000 * 3)


def test_builder_finalizes_only_closed_minutes() -> None:
    b = MinuteBuilder(0.1, 10.0)
    done = b.push(HAND.head(4))
    assert done.empty  # minute 0 still open: no later trade seen yet
    done = b.push(HAND.slice(4, 1))
    assert done.ohlcv["minute_ts"].to_list() == [T0]
    done = b.flush()
    assert done.ohlcv["minute_ts"].to_list() == [T0 + 60_000]


def test_builder_rejects_trades_for_finalized_minute() -> None:
    b = MinuteBuilder(0.1, 10.0)
    b.push(HAND)
    with pytest.raises(OutOfOrderTradeError):
        b.push(_tape([(T0 + 5, 99, 100_000.0, 1.0, "B")]))


def _run_chunks(trades: pl.DataFrame, sizes: list[int]) -> tuple[pl.DataFrame, pl.DataFrame]:
    b = MinuteBuilder(0.1, 10.0)
    fps, ohs = [], []
    off = 0
    i = 0
    while off < trades.height:
        n = sizes[i % len(sizes)]
        d = b.push(trades.slice(off, n))
        fps.append(d.footprint)
        ohs.append(d.ohlcv)
        off += n
        i += 1
    d = b.flush()
    fps.append(d.footprint)
    ohs.append(d.ohlcv)
    return pl.concat(fps), pl.concat(ohs)


TAPE = synthetic_trades(3_000, span_ms=20 * 60_000)
FULL_FP, FULL_OH = _run_chunks(TAPE, [TAPE.height])


@settings(max_examples=40, deadline=None)
@given(st.lists(st.integers(min_value=1, max_value=400), min_size=1, max_size=8))
def test_batch_size_invariance(sizes: list[int]) -> None:
    fp, oh = _run_chunks(TAPE, sizes)
    assert fp.sort("minute_ts", "bucket").equals(FULL_FP.sort("minute_ts", "bucket"))
    assert oh.equals(FULL_OH)


def test_footprint_totals_match_ohlcv() -> None:
    per_min = FULL_FP.group_by("minute_ts").agg(
        (pl.col("ask_vol") + pl.col("bid_vol")).sum().alias("v"),
        pl.col("ask_vol").sum().alias("a"),
    )
    j = per_min.join(FULL_OH, on="minute_ts")
    assert j.height == FULL_OH.height
    assert ((j["v"] - j["volume"]).abs() < 1e-9).all()
    assert ((j["a"] - j["buy_volume"]).abs() < 1e-9).all()
    assert FULL_OH["volume"].sum() == pytest.approx(TAPE["qty"].sum())
