"""Regression tests for Binance API facts verified on 2026-11-26 (docs/DATA_SOURCES.md).

The two WS frames below are recorded real frames (2026-11-26, wss://fstream.binance.com/
market). Everything else is synthetic.
"""

from __future__ import annotations

import json

import httpx
import polars as pl
import pytest

from flowlevels.settings import Settings
from flowlevels.stream.source import LiveConfig, LiveSource
from flowlevels.venues.base import RestLookbackExceededError
from flowlevels.venues.binance import (
    REST_AGGTRADES_MAX_LIMIT,
    REST_AGGTRADES_WEIGHT,
    BinanceUSDM,
    parse_rest_aggtrades,
    parse_ws_message,
)
from flowlevels.venues.verify import check_aggressor_semantics
from tests.helpers import FakeAdapter, synthetic_trades, ws_agg_frame

REAL_AGGTRADE_FRAME = {
    "stream": "btcusdt@aggTrade",
    "data": {
        "e": "aggTrade",
        "E": 1795698835891,
        "a": 3552454711,
        "s": "BTCUSDT",
        "p": "60801.40",
        "q": "0.003",
        "nq": "0.003",
        "f": 8344789507,
        "l": 8344789508,
        "T": 1795698835741,
        "m": False,
        "st": 1,
    },
}
REAL_FORCEORDER_FRAME = {
    "stream": "!forceOrder@arr",
    "data": {
        "e": "forceOrder",
        "E": 1795698837107,
        "o": {
            "s": "ZECUSDT",
            "S": "SELL",
            "o": "LIMIT",
            "f": "IOC",
            "q": "6.054",
            "p": "717.23",
            "ap": "724.35",
            "X": "FILLED",
            "l": "0.215",
            "z": "6.054",
            "T": 1795698837078,
            "ps": "ZECUSDT",
            "st": 1,
        },
    },
}


# ------------------------------------------------------------------ WS endpoint routing
def test_default_ws_url_is_routed_market_endpoint() -> None:
    assert Settings().binance_ws_url == "wss://fstream.binance.com/market"
    url = BinanceUSDM().live_url(["BTCUSDT", "ETHUSDT"])
    assert url == (
        "wss://fstream.binance.com/market/stream?streams="
        "btcusdt@aggTrade/btcusdt@forceOrder/ethusdt@aggTrade/ethusdt@forceOrder"
    )


@pytest.mark.parametrize(
    "url",
    ["wss://fstream.binance.com", "wss://fstream.binance.com/", "wss://fstream.binance.com/public"],
)
def test_unrouted_ws_url_is_rejected(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Unrouted connections silently receive no aggTrade/forceOrder data since 2026-04-23.
    monkeypatch.setenv("FLOWLEVELS_BINANCE_WS_URL", url)
    with pytest.raises(ValueError, match="/market"):
        Settings()


def test_non_binance_ws_url_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLOWLEVELS_BINANCE_WS_URL", "ws://127.0.0.1:9999")
    assert Settings().binance_ws_url == "ws://127.0.0.1:9999"


# ------------------------------------------------------------------ payloads
def test_real_aggtrade_frame_with_nq_and_st() -> None:
    b = parse_ws_message(json.dumps(REAL_AGGTRADE_FRAME))
    assert b is not None and b.kind == "trades" and b.symbol == "BTCUSDT"
    assert b.df.row(0, named=True) == {
        "ts_ms": 1795698835741,  # T, not E
        "trade_id": 3552454711,
        "price": 60801.40,
        "qty": 0.003,  # q (RPI-inclusive), not nq (D-020)
        "aggressor": "B",  # m=false -> buyer is the taker
        "fill_count": 2,
    }


def test_qty_uses_q_not_nq_when_they_differ() -> None:
    frame = json.loads(json.dumps(REAL_AGGTRADE_FRAME))
    frame["data"].update(q="0.010", nq="0.003", m=True)
    b = parse_ws_message(json.dumps(frame))
    assert b is not None
    assert b.df["qty"][0] == 0.010 and b.df["aggressor"][0] == "S"


def test_real_force_order_frame() -> None:
    b = parse_ws_message(json.dumps(REAL_FORCEORDER_FRAME))
    assert b is not None and b.kind == "liquidations" and b.symbol == "ZECUSDT"
    row = b.df.row(0, named=True)
    assert row["side"] == "S" and row["ts_ms"] == 1795698837078
    assert row["qty"] == 6.054 and row["avg_price"] == 724.35 and row["status"] == "FILLED"


def test_rest_payload_with_nq() -> None:
    payload = [
        {"a": 7, "p": "1.5", "q": "2", "nq": "1", "f": 1, "l": 2, "T": 1795698835741, "m": True}
    ]
    df = parse_rest_aggtrades(payload)
    assert df["qty"].to_list() == [2.0] and df["aggressor"].to_list() == ["S"]


# ------------------------------------------------------------------ REST limits
def _adapter(handler: httpx.MockTransport, sleeps: list[float], now: float = 0.0) -> BinanceUSDM:
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    return BinanceUSDM(
        http=httpx.AsyncClient(transport=handler),
        weight_budget_1m=1200,
        sleep=fake_sleep,
        wall_clock=lambda: now,
    )


async def test_rest_search_window_error_is_typed() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        body = {"code": -4166, "msg": "Search window is restricted to recent 2 days only."}
        return httpx.Response(400, json=body)

    a = _adapter(httpx.MockTransport(handle), [])
    with pytest.raises(RestLookbackExceededError):
        await a.fetch_trades_from_id("BTCUSDT", 1, 1000)
    await a.aclose()


@pytest.mark.parametrize(("used", "expect_wait"), [(1100, False), (1190, True)])
async def test_rest_weight_budget_pauses_until_next_minute(used: int, expect_wait: bool) -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        row = {"a": 1, "p": "1", "q": "1", "f": 1, "l": 1, "T": 1795698835741, "m": False}
        return httpx.Response(200, json=[row], headers={"X-MBX-USED-WEIGHT-1M": str(used)})

    sleeps: list[float] = []
    a = _adapter(
        httpx.MockTransport(handle), sleeps, now=1_000_000_065.0
    )  # 1_000_000_020 is a whole minute
    await a.fetch_trades_from_id("BTCUSDT", 1, 1000)
    await a.aclose()
    assert a.last_used_weight_1m == used
    assert sleeps == ([16.0] if expect_wait else [])  # 60 - 45 + 1


def test_default_backfill_pacing_respects_half_the_ip_limit() -> None:
    per_min = 60.0 / LiveConfig().backfill_pause_s * REST_AGGTRADES_WEIGHT
    assert per_min <= 1200 and LiveConfig().backfill_page == REST_AGGTRADES_MAX_LIMIT


async def test_backfill_beyond_rest_lookback_is_unresolved_with_reason() -> None:
    tape = synthetic_trades(40, first_id=1)
    frames = [ws_agg_frame(r) for r in tape.iter_rows(named=True)]
    adapter = FakeAdapter(
        tape, [[*frames[:5], *frames[20:25]]], rest_error=RestLookbackExceededError("old")
    )

    async def no_sleep(_: float) -> None:
        return None

    src = LiveSource(adapter, ["BTCUSDT"], LiveConfig(), sleep=no_sleep, max_connections=1)
    got = [b.df async for b in src]
    assert pl.concat(got)["trade_id"].to_list() == [1, 2, 3, 4, 5, 21, 22, 23, 24, 25]
    failed = [e for e in src.events if e.kind == "backfill_failed"]
    assert len(failed) == 1 and failed[0].detail["reason"] == "beyond_rest_lookback"
    gaps = src.trackers["BTCUSDT"].gaps
    assert [(g.after_id, g.before_id, g.resolved) for g in gaps] == [(5, 21, False)]


# ------------------------------------------------------------------ m semantics check
def _klines_from(trades: pl.DataFrame) -> pl.DataFrame:
    return (
        trades.with_columns((pl.col("ts_ms") // 60_000 * 60_000).alias("open_time"))
        .group_by("open_time")
        .agg(
            pl.col("qty").sum().alias("volume"),
            pl.col("qty").filter(pl.col("aggressor") == "B").sum().alias("taker_buy_volume"),
        )
        .sort("open_time")
    )


def test_aggressor_semantics_check_detects_flipped_mapping() -> None:
    trades = synthetic_trades(2000, span_ms=30 * 60_000, first_id=1, seed=3)
    klines = _klines_from(trades)
    assert check_aggressor_semantics(trades, klines).ok
    flipped = trades.with_columns(
        pl.when(pl.col("aggressor") == "B")
        .then(pl.lit("S"))
        .otherwise(pl.lit("B"))
        .alias("aggressor")
    )
    assert not check_aggressor_semantics(flipped, klines).ok
