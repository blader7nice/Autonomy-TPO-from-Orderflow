"""Test helpers: SYNTHETIC tapes and Binance-shaped files built from them.

These are not market data. They exist only to exercise parsing, aggregation and gap logic.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np
import polars as pl

from flowlevels.stream.types import TRADE_SCHEMA, Batch
from flowlevels.venues.binance import parse_ws_message

T0 = 1_767_225_600_000  # 2026-01-01 00:00:00 UTC


def synthetic_trades(
    n: int,
    seed: int = 7,
    start_ms: int = T0,
    span_ms: int = 3_600_000,
    start_price: float = 100_000.0,
    tick: float = 0.1,
    first_id: int = 1_000,
) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    ts = np.sort(rng.integers(start_ms, start_ms + span_ms, size=n))
    steps = rng.choice([-1, 0, 1], size=n, p=[0.3, 0.4, 0.3]) * rng.integers(1, 30, size=n)
    price_ticks = np.round(start_price / tick) + np.cumsum(steps)
    price = np.round(price_ticks * tick, 10)
    qty = np.round(rng.exponential(0.05, size=n) + 0.001, 3)
    aggr = np.where(rng.random(n) < 0.5, "B", "S")
    fills = rng.integers(1, 4, size=n)
    return pl.DataFrame(
        {
            "ts_ms": ts.astype(np.int64),
            "trade_id": np.arange(first_id, first_id + n, dtype=np.int64),
            "price": price,
            "qty": qty,
            "aggressor": aggr,
            "fill_count": fills.astype(np.int64),
        },
        schema=TRADE_SCHEMA,
    )


def aggtrades_csv(trades: pl.DataFrame, header: bool = True) -> bytes:
    first = [0]
    rows = []
    for r in trades.iter_rows(named=True):
        f = first[0]
        first[0] += r["fill_count"]
        rows.append(
            f"{r['trade_id']},{r['price']},{r['qty']},{f},{f + r['fill_count'] - 1},"
            f"{r['ts_ms']},{'true' if r['aggressor'] == 'S' else 'false'}"
        )
    head = (
        "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"
        if header
        else ""
    )
    return (head + "\n".join(rows) + "\n").encode()


def klines_csv_from_trades(trades: pl.DataFrame, header: bool = True) -> bytes:
    k = (
        trades.with_columns((pl.col("ts_ms") // 60_000 * 60_000).alias("open_time"))
        .group_by("open_time", maintain_order=True)
        .agg(
            pl.col("price").first().alias("open"),
            pl.col("price").max().alias("high"),
            pl.col("price").min().alias("low"),
            pl.col("price").last().alias("close"),
            pl.col("qty").sum().alias("volume"),
            pl.col("fill_count").sum().alias("count"),
            pl.col("qty").filter(pl.col("aggressor") == "B").sum().alias("tbv"),
        )
    )
    lines = []
    if header:
        lines.append(
            "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
            "taker_buy_volume,taker_buy_quote_volume,ignore"
        )
    for r in k.iter_rows(named=True):
        lines.append(
            f"{r['open_time']},{r['open']},{r['high']},{r['low']},{r['close']},{r['volume']},"
            f"{r['open_time'] + 59_999},0,{r['count']},{r['tbv']},0,0"
        )
    return ("\n".join(lines) + "\n").encode()


def zip_bytes(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, data)
    return buf.getvalue()


def checksum_text(data: bytes, name: str) -> str:
    return f"{hashlib.sha256(data).hexdigest()}  {name}\n"


def ws_agg_frame(row: dict[str, object], symbol: str = "BTCUSDT") -> str:
    """A combined-stream aggTrade frame in the (UNVERIFIED) documented shape."""
    tid = int(row["trade_id"])  # type: ignore[call-overload]
    return json.dumps(
        {
            "stream": f"{symbol.lower()}@aggTrade",
            "data": {
                "e": "aggTrade",
                "E": int(row["ts_ms"]) + 5,  # type: ignore[call-overload]
                "s": symbol,
                "a": tid,
                "p": f"{row['price']}",
                "q": f"{row['qty']}",
                "f": tid * 10,
                "l": tid * 10 + int(row["fill_count"]) - 1,  # type: ignore[call-overload]
                "T": int(row["ts_ms"]),  # type: ignore[call-overload]
                "m": row["aggressor"] == "S",
            },
        }
    )


class FakeAdapter:
    """VenueAdapter double: scripted WS connections + REST served from a full tape."""

    name = "fake"

    def __init__(
        self,
        tape: pl.DataFrame,
        connections: list[list[str | Exception]],
        symbol: str = "BTCUSDT",
        rest_fail: bool = False,
    ) -> None:
        self.tape = tape
        self.connections = connections
        self.symbol = symbol
        self.rest_calls: list[int] = []
        self.rest_fail = rest_fail
        self.n_connect = 0

    def live_url(self, symbols: list[str], liquidations: bool = True) -> str:
        return "fake://" + ",".join(symbols)

    def parse_live_message(self, raw: str | bytes) -> Batch | None:
        return parse_ws_message(raw)

    async def live_messages(self, url: str) -> AsyncIterator[str | bytes]:
        script = self.connections[min(self.n_connect, len(self.connections) - 1)]
        self.n_connect += 1
        for item in script:
            if isinstance(item, Exception):
                raise item
            if item.startswith("sleep:"):
                import asyncio

                await asyncio.sleep(float(item[6:]))
                continue
            yield item

    async def fetch_trades_from_id(self, symbol: str, from_id: int, limit: int) -> pl.DataFrame:
        self.rest_calls.append(from_id)
        if self.rest_fail:
            raise ConnectionError("rest down")
        return self.tape.filter(pl.col("trade_id") >= from_id).head(limit)

    # historical methods unused by live tests
    async def download_trades_day(self, *a: object) -> Path:  # pragma: no cover
        raise NotImplementedError

    def read_trades_file(self, path: Path) -> pl.DataFrame:  # pragma: no cover
        raise NotImplementedError

    async def download_klines_1m_day(self, *a: object) -> Path:  # pragma: no cover
        raise NotImplementedError

    def read_klines_file(self, path: Path) -> pl.DataFrame:  # pragma: no cover
        raise NotImplementedError
