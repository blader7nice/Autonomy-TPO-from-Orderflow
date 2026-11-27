"""Repair aggTrade archive holes from the raw `trades` archive (D-032).

The aggTrades archive can lack trades while its aggTrade ids stay contiguous (2026-10-27,
D-026). The raw per-fill `trades` archive of the same day is complete, and every aggTrade
carries the raw fill-id range [f, l] it aggregates. For a hole (after_ms, before_ms):

- L = max `l` of the aggTrades at or before after_ms, F = min `f` of those at or after
  before_ms; the missing fills are the raw trades with L < id < F;
- they are verified against the day's 1m klines: every minute strictly inside the hole must
  match the kline volume, taker-buy volume and count exactly, and the whole day (aggTrades
  + fills) must match the kline day totals (volume and taker-buy volume);
- only verified fills are used. They go through their own `Ingestor` (the same footprint
  code path, with the raw fill id as `trade_id`, so no id namespace is mixed), and the
  resulting minutes are merged additively into the aggTrade minutes (`merge_minutes`).

If anything fails, the hole stays unrepaired and the D-031 exclusion rule applies to it.
Every repair is recorded with its source file and checksum in the completion marker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from flowlevels.footprint.minute import FOOTPRINT_SCHEMA, MINUTE_MS, OHLCV_SCHEMA
from flowlevels.stream.types import TRADE_SCHEMA
from flowlevels.venues.base import DataNotAvailableError, VenueAdapter
from flowlevels.venues.binance import sha256_file

log = logging.getLogger(__name__)

REL_TOL = 1e-9


@dataclass
class RepairOutcome:
    fills: pl.DataFrame  # verified fills of all repaired holes, TRADE_SCHEMA
    repaired: list[dict[str, Any]] = field(default_factory=list)
    unrepaired: list[dict[str, Any]] = field(default_factory=list)


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= REL_TOL * max(abs(a), abs(b), 1.0)


def hole_fills(
    agg: pl.DataFrame, fills: pl.DataFrame, after_ms: int, before_ms: int
) -> pl.DataFrame:
    """Raw fills that belong in the hole, by fill-id range (see module doc)."""
    before = agg.filter(pl.col("ts_ms") <= after_ms)
    after = agg.filter(pl.col("ts_ms") >= before_ms)
    lo = int(before["last_fill_id"].max()) if before.height else -1  # type: ignore[arg-type]
    hi = int(after["first_fill_id"].min()) if after.height else 2**62  # type: ignore[arg-type]
    return fills.filter((pl.col("trade_id") > lo) & (pl.col("trade_id") < hi))


def _kline_minutes(klines: pl.DataFrame) -> pl.DataFrame:
    return klines.select(
        pl.col("open_time").alias("minute_ts"),
        pl.col("volume").alias("k_volume"),
        pl.col("taker_buy_volume").alias("k_buy"),
        pl.col("count").alias("k_count"),
    )


def check_hole(
    fills: pl.DataFrame, klines: pl.DataFrame, after_ms: int, before_ms: int
) -> dict[str, Any]:
    """Compare fills with klines on the minutes strictly inside (after_ms, before_ms)."""
    first_in = (after_ms // MINUTE_MS + 1) * MINUTE_MS
    last_out = before_ms // MINUTE_MS * MINUTE_MS  # minutes [first_in, last_out) are interior
    f = (
        fills.filter((pl.col("ts_ms") >= first_in) & (pl.col("ts_ms") < last_out))
        .with_columns((pl.col("ts_ms") // MINUTE_MS * MINUTE_MS).alias("minute_ts"))
        .group_by("minute_ts")
        .agg(
            pl.col("qty").sum().alias("volume"),
            pl.col("qty").filter(pl.col("aggressor") == "B").sum().alias("buy"),
            pl.len().alias("count"),
        )
    )
    k = _kline_minutes(klines).filter(
        (pl.col("minute_ts") >= first_in)
        & (pl.col("minute_ts") < last_out)
        & (pl.col("k_volume") > 0)
    )
    j = k.join(f, on="minute_ts", how="full", coalesce=True).fill_null(0)
    bad = [
        int(r["minute_ts"])
        for r in j.iter_rows(named=True)
        if not (
            _close(r["volume"], r["k_volume"])
            and _close(r["buy"], r["k_buy"])
            and int(r["count"]) == int(r["k_count"])
        )
    ]
    # Flipped aggressor mapping as a negative control: it must NOT match (unless no flow).
    flipped = sum(
        _close(r["volume"] - r["buy"], r["k_buy"])
        for r in j.iter_rows(named=True)
        if r["k_volume"] > 0
    )
    return {
        "interior_minutes": j.height,
        "interior_mismatched": len(bad),
        "mismatched_minute_ts": bad[:20],
        "flipped_aggressor_matches": int(flipped),
        "fills": fills.height,
        "volume": float(fills["qty"].sum()),
        "buy_volume": float(fills.filter(pl.col("aggressor") == "B")["qty"].sum()),
    }


def day_totals_check(
    agg: pl.DataFrame, fills: pl.DataFrame, klines: pl.DataFrame
) -> dict[str, Any]:
    vol = float(agg["qty"].sum()) + float(fills["qty"].sum())
    buy = float(agg.filter(pl.col("aggressor") == "B")["qty"].sum()) + float(
        fills.filter(pl.col("aggressor") == "B")["qty"].sum()
    )
    kv, kb = float(klines["volume"].sum()), float(klines["taker_buy_volume"].sum())
    return {
        "volume": [vol, kv],
        "buy_volume": [buy, kb],
        "ok": _close(vol, kv) and _close(buy, kb),
    }


async def repair_day_holes(
    adapter: VenueAdapter,
    symbol: str,
    day: Any,
    agg: pl.DataFrame,
    holes: list[dict[str, Any]],
    raw_dir: Path,
    keep_raw: bool = False,
) -> RepairOutcome:
    """Try to repair `holes` of one day; `agg` must carry first/last_fill_id columns."""
    empty = pl.DataFrame(schema=TRADE_SCHEMA)
    if not holes:
        return RepairOutcome(empty)
    try:
        tpath = await adapter.download_raw_trades_day(symbol, day, raw_dir)
        kpath = await adapter.download_klines_1m_day(symbol, day, raw_dir)
    except DataNotAvailableError as exc:
        return RepairOutcome(
            empty, [], [{**h, "repair_error": f"source missing: {exc}"} for h in holes]
        )
    source = {"file": tpath.name, "sha256": sha256_file(tpath)}
    all_fills = adapter.read_raw_trades_file(tpath)
    klines = adapter.read_klines_file(kpath)
    if not keep_raw:
        tpath.unlink(missing_ok=True)
        kpath.unlink(missing_ok=True)
    out = RepairOutcome(empty)
    good: list[pl.DataFrame] = []
    for h in holes:
        fills = hole_fills(agg, all_fills, int(h["after_ms"]), int(h["before_ms"]))
        chk = check_hole(fills, klines, int(h["after_ms"]), int(h["before_ms"]))
        if fills.height == 0 or chk["interior_mismatched"]:
            out.unrepaired.append({**h, "repair_error": "kline check failed", "check": chk})
            continue
        good.append(fills)
        out.repaired.append(
            {
                **h,
                "source": "binance data.binance.vision raw trades archive",
                "source_file": source["file"],
                "source_sha256": source["sha256"],
                "check": chk,
            }
        )
    fills = pl.concat(good).sort("trade_id") if good else empty
    if out.repaired:
        tot = day_totals_check(agg, fills, klines)
        if not tot["ok"]:
            log.warning("%s %s: day totals do not match klines after repair: %s", symbol, day, tot)
            out.unrepaired += [
                {k: v for k, v in r.items() if not k.startswith("source")}
                | {"repair_error": "day totals check failed", "day_totals": tot}
                for r in out.repaired
            ]
            out.repaired, fills = [], empty
        else:
            for r in out.repaired:
                r["day_totals"] = tot
    out.fills = fills
    return out


def merge_minutes(
    fp_a: pl.DataFrame,
    oh_a: pl.DataFrame,
    trades_a: pl.DataFrame,
    fp_b: pl.DataFrame,
    oh_b: pl.DataFrame,
    trades_b: pl.DataFrame,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Additive merge of two minute sets built from disjoint trade sets (a = aggTrades).

    Footprint: sums of sums, max of maxes. OHLCV: open from the source whose first trade in
    the minute is earlier, close from the one whose last trade is later; first/last_id stay
    those of the aggTrades (null in minutes that only have fills, so no fill id can ever be
    taken for an aggTrade id).
    """
    fp = (
        pl.concat([fp_a, fp_b])
        .group_by("minute_ts", "bucket")
        .agg(
            pl.col("ask_vol").sum(),
            pl.col("bid_vol").sum(),
            pl.col("ask_trades").sum(),
            pl.col("bid_trades").sum(),
            pl.col("ask_max_trade").max(),
            pl.col("bid_max_trade").max(),
        )
        .sort("minute_ts", "bucket")
        .cast(FOOTPRINT_SCHEMA)  # type: ignore[arg-type]
        .select(list(FOOTPRINT_SCHEMA))
    )

    def span(t: pl.DataFrame, tag: str) -> pl.DataFrame:
        return (
            t.with_columns((pl.col("ts_ms") // MINUTE_MS * MINUTE_MS).alias("minute_ts"))
            .group_by("minute_ts")
            .agg(pl.col("ts_ms").min().alias(f"t0_{tag}"), pl.col("ts_ms").max().alias(f"t1_{tag}"))
        )

    a = oh_a.join(span(trades_a, "a"), on="minute_ts", how="left")
    b = oh_b.join(span(trades_b, "b"), on="minute_ts", how="left")
    j = a.join(b, on="minute_ts", how="full", coalesce=True, suffix="_b")
    a_first = pl.col("t0_b").is_null() | (
        pl.col("t0_a").is_not_null() & (pl.col("t0_a") <= pl.col("t0_b"))
    )
    a_last = pl.col("t1_b").is_null() | (
        pl.col("t1_a").is_not_null() & (pl.col("t1_a") >= pl.col("t1_b"))
    )

    def add(c: str) -> pl.Expr:
        return (pl.col(c).fill_null(0) + pl.col(f"{c}_b").fill_null(0)).alias(c)

    oh = (
        j.select(
            "minute_ts",
            pl.when(a_first).then(pl.col("open")).otherwise(pl.col("open_b")).alias("open"),
            pl.max_horizontal("high", "high_b").alias("high"),
            pl.min_horizontal("low", "low_b").alias("low"),
            pl.when(a_last).then(pl.col("close")).otherwise(pl.col("close_b")).alias("close"),
            *(
                add(c)
                for c in (
                    "volume",
                    "buy_volume",
                    "sell_volume",
                    "n_trades",
                    "n_fills",
                    "sum_pv",
                    "sum_p2v",
                )
            ),
            pl.col("first_id"),
            pl.col("last_id"),
        )
        .sort("minute_ts")
        .cast(OHLCV_SCHEMA)  # type: ignore[arg-type]
        .select(list(OHLCV_SCHEMA))
    )
    return fp, oh
