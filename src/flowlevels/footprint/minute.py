"""Incremental 1m footprint + OHLCV builder (the base resolution).

A minute is finalized when the first trade of a later minute arrives (exchange time only),
or on flush() at the end of a finite stream. Results are independent of how the input is
split into batches (tested).

Footprint row (per minute x price bucket) stores additive quantities and per-side maxima
so that any higher resolution can be aggregated exactly (sum of sums, max of maxes).
OHLCV row (per minute) additionally stores sum(p*v) and sum(p^2*v) so that VWAP and its
volume-weighted sigma bands are exact, not approximated from closes.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from flowlevels.stream.types import TRADE_SCHEMA, empty_trades

MINUTE_MS = 60_000

FOOTPRINT_SCHEMA: dict[str, pl.DataType] = {
    "minute_ts": pl.Int64(),  # minute start, epoch ms UTC
    "bucket": pl.Int64(),  # bucket index; bucket price = bucket * bucket_size
    "ask_vol": pl.Float64(),  # buyer-aggressor volume ("lifting the ask")
    "bid_vol": pl.Float64(),  # seller-aggressor volume ("hitting the bid")
    "ask_trades": pl.Int64(),
    "bid_trades": pl.Int64(),
    "ask_max_trade": pl.Float64(),  # largest single aggTrade qty (0 if none)
    "bid_max_trade": pl.Float64(),
}

OHLCV_SCHEMA: dict[str, pl.DataType] = {
    "minute_ts": pl.Int64(),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Float64(),
    "buy_volume": pl.Float64(),  # buyer-aggressor
    "sell_volume": pl.Float64(),  # seller-aggressor
    "n_trades": pl.Int64(),  # aggTrades
    "n_fills": pl.Int64(),  # underlying trades (comparable to kline `count`)
    "sum_pv": pl.Float64(),
    "sum_p2v": pl.Float64(),
    "first_id": pl.Int64(),
    "last_id": pl.Int64(),
}


def bucket_expr(exchange_tick: float, bucket_size: float) -> pl.Expr:
    """Bucket index = floor(price_in_ticks / ticks_per_bucket), integer-exact."""
    ticks_per_bucket = round(bucket_size / exchange_tick)
    price_ticks = (pl.col("price") / exchange_tick).round(0).cast(pl.Int64)
    return (price_ticks // ticks_per_bucket).alias("bucket")


def footprint_from_trades(
    trades: pl.DataFrame, exchange_tick: float, bucket_size: float
) -> pl.DataFrame:
    if trades.height == 0:
        return pl.DataFrame(schema=FOOTPRINT_SCHEMA)
    is_ask = pl.col("aggressor") == "B"
    is_bid = pl.col("aggressor") == "S"
    return (
        trades.with_columns(
            (pl.col("ts_ms") // MINUTE_MS * MINUTE_MS).alias("minute_ts"),
            bucket_expr(exchange_tick, bucket_size),
        )
        .group_by("minute_ts", "bucket")
        .agg(
            pl.col("qty").filter(is_ask).sum().alias("ask_vol"),
            pl.col("qty").filter(is_bid).sum().alias("bid_vol"),
            is_ask.sum().cast(pl.Int64).alias("ask_trades"),
            is_bid.sum().cast(pl.Int64).alias("bid_trades"),
            pl.col("qty").filter(is_ask).max().fill_null(0.0).alias("ask_max_trade"),
            pl.col("qty").filter(is_bid).max().fill_null(0.0).alias("bid_max_trade"),
        )
        .sort("minute_ts", "bucket")
        .cast(FOOTPRINT_SCHEMA)  # type: ignore[arg-type]
        .select(list(FOOTPRINT_SCHEMA))
    )


def ohlcv_from_trades(trades: pl.DataFrame) -> pl.DataFrame:
    if trades.height == 0:
        return pl.DataFrame(schema=OHLCV_SCHEMA)
    return (
        trades.sort("trade_id")
        .with_columns((pl.col("ts_ms") // MINUTE_MS * MINUTE_MS).alias("minute_ts"))
        .group_by("minute_ts", maintain_order=True)
        .agg(
            pl.col("price").first().alias("open"),
            pl.col("price").max().alias("high"),
            pl.col("price").min().alias("low"),
            pl.col("price").last().alias("close"),
            pl.col("qty").sum().alias("volume"),
            pl.col("qty").filter(pl.col("aggressor") == "B").sum().alias("buy_volume"),
            pl.col("qty").filter(pl.col("aggressor") == "S").sum().alias("sell_volume"),
            pl.len().cast(pl.Int64).alias("n_trades"),
            pl.col("fill_count").sum().alias("n_fills"),
            (pl.col("price") * pl.col("qty")).sum().alias("sum_pv"),
            (pl.col("price") ** 2 * pl.col("qty")).sum().alias("sum_p2v"),
            pl.col("trade_id").first().alias("first_id"),
            pl.col("trade_id").last().alias("last_id"),
        )
        .sort("minute_ts")
        .cast(OHLCV_SCHEMA)  # type: ignore[arg-type]
        .select(list(OHLCV_SCHEMA))
    )


@dataclass(frozen=True, slots=True)
class FinalizedMinutes:
    footprint: pl.DataFrame
    ohlcv: pl.DataFrame

    @property
    def empty(self) -> bool:
        return self.ohlcv.height == 0


class OutOfOrderTradeError(RuntimeError):
    pass


class MinuteBuilder:
    """Turns an ordered trade stream (batches) into finalized 1m footprint/OHLCV rows."""

    def __init__(self, exchange_tick: float, bucket_size: float) -> None:
        self.exchange_tick = exchange_tick
        self.bucket_size = bucket_size
        self._open: pl.DataFrame = empty_trades()  # trades of the open (latest) minute
        self._last_finalized_minute: int | None = None

    @property
    def open_minute(self) -> int | None:
        if self._open.height == 0:
            return None
        return int(self._open["ts_ms"][0]) // MINUTE_MS * MINUTE_MS

    def push(self, trades: pl.DataFrame) -> FinalizedMinutes:
        if trades.height == 0:
            return self._finalize(empty_trades())
        first_minute = int(trades["ts_ms"].min()) // MINUTE_MS * MINUTE_MS  # type: ignore[arg-type]
        if self._last_finalized_minute is not None and first_minute <= self._last_finalized_minute:
            raise OutOfOrderTradeError(
                f"trade in minute {first_minute} after minute "
                f"{self._last_finalized_minute} was finalized"
            )
        buf = pl.concat([self._open, trades.select(list(TRADE_SCHEMA))], how="vertical")
        last_minute = int(buf["ts_ms"].max()) // MINUTE_MS * MINUTE_MS  # type: ignore[arg-type]
        done = buf.filter(pl.col("ts_ms") < last_minute)
        self._open = buf.filter(pl.col("ts_ms") >= last_minute)
        return self._finalize(done)

    def flush(self) -> FinalizedMinutes:
        """Finalize the open minute (end of a finite stream, e.g. end of a history day)."""
        done, self._open = self._open, empty_trades()
        return self._finalize(done)

    def _finalize(self, done: pl.DataFrame) -> FinalizedMinutes:
        if done.height:
            self._last_finalized_minute = (
                int(done["ts_ms"].max()) // MINUTE_MS * MINUTE_MS  # type: ignore[arg-type]
            )
        return FinalizedMinutes(
            footprint=footprint_from_trades(done, self.exchange_tick, self.bucket_size),
            ohlcv=ohlcv_from_trades(done),
        )
