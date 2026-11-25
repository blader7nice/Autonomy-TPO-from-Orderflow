"""Normalized market-event types shared by history (replay) and live.

Trades travel through the system as polars DataFrames with TRADE_SCHEMA, sorted by
trade_id (batches; see docs/DECISIONS.md D-010). A batch may hold a single trade (live) or
millions (replay); every consumer must give identical results regardless of batch size.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import polars as pl

Aggressor = Literal["B", "S"]

TRADE_SCHEMA: dict[str, pl.DataType] = {
    "ts_ms": pl.Int64(),  # exchange trade time T, ms since epoch (UTC)
    "trade_id": pl.Int64(),  # aggregate trade id `a` (sequential per symbol)
    "price": pl.Float64(),
    "qty": pl.Float64(),
    "aggressor": pl.String(),  # 'B' buyer-initiated, 'S' seller-initiated
    "fill_count": pl.Int64(),  # number of underlying fills (l - f + 1)
}

LIQUIDATION_SCHEMA: dict[str, pl.DataType] = {
    "ts_ms": pl.Int64(),  # order trade time T
    "side": pl.String(),  # 'B' = liquidation BUY order (shorts liquidated), 'S' = SELL
    "price": pl.Float64(),  # order price
    "avg_price": pl.Float64(),
    "qty": pl.Float64(),  # filled accumulated quantity
    "status": pl.String(),
}

# Plausible range for epoch milliseconds (2001-09-09 .. 2286-11-20). Anything outside is
# a unit error (e.g. microseconds) and must fail loudly.
MS_MIN = 1_000_000_000_000
MS_MAX = 10_000_000_000_000


def aggressor_from_is_buyer_maker(is_buyer_maker: bool) -> Aggressor:
    """Binance `m`: True => buyer is the maker => the aggressor (taker) is the SELLER."""
    return "S" if is_buyer_maker else "B"


def aggressor_expr(is_buyer_maker_col: str) -> pl.Expr:
    """Vectorized version of :func:`aggressor_from_is_buyer_maker`."""
    return (
        pl.when(pl.col(is_buyer_maker_col))
        .then(pl.lit("S"))
        .otherwise(pl.lit("B"))
        .alias("aggressor")
    )


def empty_trades() -> pl.DataFrame:
    return pl.DataFrame(schema=TRADE_SCHEMA)


def empty_liquidations() -> pl.DataFrame:
    return pl.DataFrame(schema=LIQUIDATION_SCHEMA)


def validate_trades(df: pl.DataFrame) -> pl.DataFrame:
    """Check schema, ms timestamps, aggressor values and id ordering. Returns df."""
    if dict(df.schema) != TRADE_SCHEMA:
        raise ValueError(f"Bad trade schema: {dict(df.schema)}")
    if df.height == 0:
        return df
    ts_min, ts_max = df["ts_ms"].min(), df["ts_ms"].max()
    assert isinstance(ts_min, int) and isinstance(ts_max, int)
    if ts_min < MS_MIN or ts_max >= MS_MAX:
        raise ValueError(f"Timestamps not in epoch ms range: {ts_min}..{ts_max}")
    bad = df.filter(~pl.col("aggressor").is_in(["B", "S"])).height
    if bad:
        raise ValueError(f"{bad} rows with invalid aggressor")
    if df.height > 1 and not bool((df["trade_id"].diff().drop_nulls() > 0).all()):
        raise ValueError("trade_id must be strictly increasing within a batch")
    return df


Kind = Literal["trades", "liquidations"]


@dataclass(frozen=True, slots=True)
class Batch:
    """A batch of normalized events for one symbol."""

    symbol: str
    kind: Kind
    df: pl.DataFrame
