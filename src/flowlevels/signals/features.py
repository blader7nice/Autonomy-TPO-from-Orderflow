"""Per-bar footprint features at a signal resolution, and time-of-day robust baselines.

Everything here is a function of closed 1m rows only: a bar's features are known at its
close (bar_ts + res). Bars are aggregated exactly from the 1m footprint/OHLCV
(`footprint.bars`), and the imbalance rules are those of D-015 (vectorized here; a test
checks them against `summarize_bar`).

Columns of `bar_features`:
- bar_ts, open, high, low, close, volume, delta (buy - sell aggressor volume);
- close_pos: (close - low) / (high - low), 0.5 for a zero-range bar;
- n_buckets: ladder length (contiguous, untraded buckets inside the range count);
- poc_vol, low_vol, high_vol: total volume at the bar POC, lowest and highest bucket;
- stk_buy_low: a stacked BUY imbalance whose centre is in the lower half of the ladder;
- stk_sell_high: a stacked SELL imbalance whose centre is in the upper half.

Baselines (SPEC 7, D-006): for a bar on day D in time-of-day slot s (baseline_slot_minutes),
median and MAD of the same slot's bars on the previous `baseline_days` calendar days of the
same kind (weekday vs weekend), excluding bars that overlap an unrepaired data hole.
robust z = (x - median) / (1.4826 MAD); None with fewer than baseline_min_samples samples
or MAD = 0. Only days strictly before D are used.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from flowlevels.config import FootprintConfig
from flowlevels.footprint.bars import aggregate_footprint, aggregate_ohlcv, res_ms

DAY_MS = 86_400_000


def bar_features(
    oh_1m: pl.DataFrame, fp_1m: pl.DataFrame, res: str, fc: FootprintConfig
) -> pl.DataFrame:
    oh = aggregate_ohlcv(oh_1m, res).select(
        "bar_ts", "open", "high", "low", "close", "volume", "delta", "n_minutes"
    )
    fp = aggregate_footprint(fp_1m, res).with_columns(
        (pl.col("ask_vol") + pl.col("bid_vol")).alias("tot")
    )
    ladder = fp.group_by("bar_ts").agg(
        pl.col("bucket").min().alias("b_lo"),
        pl.col("bucket").max().alias("b_hi"),
        pl.col("tot").max().alias("poc_vol"),
        pl.col("tot").sort_by("bucket").first().alias("low_vol"),
        pl.col("tot").sort_by("bucket").last().alias("high_vol"),
    )
    stk = _stacked(fp, ladder, fc)
    out = (
        oh.join(ladder, on="bar_ts", how="left")
        .join(stk, on="bar_ts", how="left")
        .with_columns(
            (pl.col("b_hi") - pl.col("b_lo") + 1).alias("n_buckets"),
            pl.when(pl.col("high") > pl.col("low"))
            .then((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low")))
            .otherwise(0.5)
            .alias("close_pos"),
            pl.col("stk_buy_low").fill_null(False),
            pl.col("stk_sell_high").fill_null(False),
        )
        .drop("b_lo", "b_hi")
        .sort("bar_ts")
    )
    return out


def _stacked(fp: pl.DataFrame, ladder: pl.DataFrame, fc: FootprintConfig) -> pl.DataFrame:
    """Stacked imbalances per bar (D-015), located relative to the ladder middle."""
    below = fp.select(
        "bar_ts", (pl.col("bucket") + 1).alias("bucket"), pl.col("bid_vol").alias("bid_below")
    )
    above = fp.select(
        "bar_ts", (pl.col("bucket") - 1).alias("bucket"), pl.col("ask_vol").alias("ask_above")
    )
    x = (
        fp.join(below, on=["bar_ts", "bucket"], how="left")
        .join(above, on=["bar_ts", "bucket"], how="left")
        .join(ladder.select("bar_ts", "b_lo", "b_hi"), on="bar_ts")
        .with_columns(pl.col("bid_below").fill_null(0.0), pl.col("ask_above").fill_null(0.0))
    )
    r, mv = fc.imb_ratio, fc.imb_min_volume
    # ask[p] vs bid[p-1] exists only for p > lowest bucket; bid[p] vs ask[p+1] for p < highest.
    buy = (
        (pl.col("bucket") > pl.col("b_lo"))
        & (pl.col("ask_vol") > 0)
        & (pl.col("ask_vol") >= mv)
        & (pl.col("ask_vol") >= r * pl.col("bid_below"))
    )
    sell = (
        (pl.col("bucket") < pl.col("b_hi"))
        & (pl.col("bid_vol") > 0)
        & (pl.col("bid_vol") >= mv)
        & (pl.col("bid_vol") >= r * pl.col("ask_above"))
    )
    x = x.with_columns(buy.alias("buy"), sell.alias("sell"))
    mid = (pl.col("b_lo") + pl.col("b_hi")) / 2.0

    def runs(flag: str) -> pl.DataFrame:
        f = x.filter(pl.col(flag)).sort("bar_ts", "bucket")
        f = f.with_columns(
            (
                (pl.col("bucket").diff().over("bar_ts").fill_null(0) != 1)
                | (pl.col("bar_ts").diff().fill_null(1) != 0)
            )
            .cum_sum()
            .alias("run")
        )
        return f.group_by("bar_ts", "run").agg(
            pl.len().alias("n"),
            ((pl.col("bucket").min() + pl.col("bucket").max()) / 2.0).alias("centre"),
            mid.first().alias("mid"),
        )

    rb = runs("buy").filter(pl.col("n") >= fc.stack_n)
    rs = runs("sell").filter(pl.col("n") >= fc.stack_n)
    sb = (
        rb.filter(pl.col("centre") <= pl.col("mid"))
        .select("bar_ts")
        .unique()
        .with_columns(pl.lit(True).alias("stk_buy_low"))
    )
    ss = (
        rs.filter(pl.col("centre") >= pl.col("mid"))
        .select("bar_ts")
        .unique()
        .with_columns(pl.lit(True).alias("stk_sell_high"))
    )
    return (
        ladder.select("bar_ts").join(sb, on="bar_ts", how="left").join(ss, on="bar_ts", how="left")
    )


def _weekend(day_idx: int) -> bool:
    return (dt.date(1970, 1, 1) + dt.timedelta(days=day_idx)).weekday() >= 5


def add_baselines(
    bars: pl.DataFrame,
    fc: FootprintConfig,
    cols: tuple[str, ...] = ("volume", "delta"),
    excluded: pl.Series | None = None,
) -> pl.DataFrame:
    """Add `<col>_z` robust z-scores (see module doc). `excluded`: bars not used as samples."""
    slot_ms = fc.baseline_slot_minutes * 60_000
    b = bars.with_columns(
        (pl.col("bar_ts") // DAY_MS).alias("_day"),
        ((pl.col("bar_ts") % DAY_MS) // slot_ms).alias("_slot"),
    )
    days = b["_day"].unique().sort().to_list()
    wk = {d: _weekend(d) for d in range(min(days) - fc.baseline_days, max(days) + 1)}
    b = b.with_columns(pl.col("_day").replace_strict(wk, return_dtype=pl.Boolean).alias("_we"))
    samples = b if excluded is None else b.filter(~excluded)
    parts = []
    for d in days:
        hist = samples.filter(
            (pl.col("_day") < d)
            & (pl.col("_day") >= d - fc.baseline_days)
            & (pl.col("_we") == wk[d])
        )
        stats = hist.group_by("_slot").agg(
            pl.len().alias("_n"),
            *(pl.col(c).median().alias(f"_med_{c}") for c in cols),
            *((pl.col(c) - pl.col(c).median()).abs().median().alias(f"_mad_{c}") for c in cols),
        )
        parts.append(b.filter(pl.col("_day") == d).join(stats, on="_slot", how="left"))
    x = pl.concat(parts, how="diagonal_relaxed").sort("bar_ts")
    ok = pl.col("_n").fill_null(0) >= fc.baseline_min_samples
    x = x.with_columns(
        pl.when(ok & (pl.col(f"_mad_{c}") > 0))
        .then((pl.col(c) - pl.col(f"_med_{c}")) / (1.4826 * pl.col(f"_mad_{c}")))
        .otherwise(None)
        .alias(f"{c}_z")
        for c in cols
    )
    return x.drop([c for c in x.columns if c.startswith("_")])


def bar_ms(res: str) -> int:
    return res_ms(res)
