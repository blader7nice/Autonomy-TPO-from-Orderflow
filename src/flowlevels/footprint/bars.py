"""Higher-resolution footprint bars aggregated from 1m rows, plus bar statistics.

Bars are aligned to UTC epoch multiples of the resolution (5m bars start at :00, :05, ...;
1h bars at the full hour). A bar is complete when its last minute is complete, i.e. at
bar_ts + res. Aggregation is exact: sums of sums, max of maxes.

Imbalance rules (documented in docs/DECISIONS.md D-015):
- The ladder of a bar spans every bucket between its lowest and highest traded bucket;
  untraded buckets inside the range have zero volume.
- Buy imbalance at bucket p: ask[p] >= imb_ratio * bid[p-1] and ask[p] >= imb_min_volume
  and ask[p] > 0 (bid[p-1] == 0 with ask[p] > 0 counts as an imbalance).
- Sell imbalance at bucket p: bid[p] >= imb_ratio * ask[p+1], same conditions mirrored.
- Stacked imbalance: >= stack_n consecutive buckets with the same-side imbalance.
- Bar POC: bucket with max total volume; ties -> closest to the bar's mid bucket, then the
  lower bucket.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

from flowlevels.footprint.minute import MINUTE_MS

RESOLUTIONS_MIN = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60}


def res_ms(res: str) -> int:
    try:
        return RESOLUTIONS_MIN[res] * MINUTE_MS
    except KeyError as exc:
        raise ValueError(f"Unknown resolution {res!r}; known: {list(RESOLUTIONS_MIN)}") from exc


def aggregate_footprint(fp_1m: pl.DataFrame, res: str) -> pl.DataFrame:
    """1m footprint rows -> footprint rows at `res` (bar_ts, bucket, ...)."""
    step = res_ms(res)
    return (
        fp_1m.with_columns((pl.col("minute_ts") // step * step).alias("bar_ts"))
        .group_by("bar_ts", "bucket")
        .agg(
            pl.col("ask_vol").sum(),
            pl.col("bid_vol").sum(),
            pl.col("ask_trades").sum(),
            pl.col("bid_trades").sum(),
            pl.col("ask_max_trade").max(),
            pl.col("bid_max_trade").max(),
        )
        .sort("bar_ts", "bucket")
    )


def aggregate_ohlcv(ohlcv_1m: pl.DataFrame, res: str) -> pl.DataFrame:
    """1m OHLCV -> OHLCV at `res`, with delta and CVD (continuous + UTC-session reset)."""
    step = res_ms(res)
    bars = (
        ohlcv_1m.sort("minute_ts")
        .with_columns((pl.col("minute_ts") // step * step).alias("bar_ts"))
        .group_by("bar_ts", maintain_order=True)
        .agg(
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
            pl.col("volume").sum(),
            pl.col("buy_volume").sum(),
            pl.col("sell_volume").sum(),
            pl.col("n_trades").sum(),
            pl.col("sum_pv").sum(),
            pl.col("sum_p2v").sum(),
            pl.col("first_id").first(),
            pl.col("last_id").last(),
            pl.len().alias("n_minutes"),
        )
    )
    return add_delta_cvd(bars, "bar_ts")


def add_delta_cvd(bars: pl.DataFrame, ts_col: str) -> pl.DataFrame:
    """delta, continuous CVD and CVD reset at each UTC day (session)."""
    return bars.with_columns(
        (pl.col("buy_volume") - pl.col("sell_volume")).alias("delta"),
    ).with_columns(
        pl.col("delta").cum_sum().alias("cvd"),
        pl.col("delta").cum_sum().over(pl.col(ts_col) // 86_400_000).alias("cvd_session"),
    )


@dataclass(frozen=True, slots=True)
class Ladder:
    """One bar's footprint ladder, contiguous buckets from low to high."""

    bar_ts: int
    buckets: np.ndarray  # int64, ascending, contiguous
    ask: np.ndarray
    bid: np.ndarray
    ask_trades: np.ndarray
    bid_trades: np.ndarray

    @property
    def total(self) -> np.ndarray:
        return np.asarray(self.ask + self.bid)

    @property
    def delta(self) -> float:
        return float(self.ask.sum() - self.bid.sum())

    def poc_bucket(self) -> int:
        tot = self.total
        mx = tot.max()
        cand = np.flatnonzero(tot == mx)
        mid = (len(tot) - 1) / 2.0
        best = min(cand, key=lambda i: (abs(i - mid), i))
        return int(self.buckets[best])


def ladder(fp_bar: pl.DataFrame, bar_ts: int) -> Ladder:
    """Build a contiguous ladder for one bar from its footprint rows (any resolution)."""
    rows = fp_bar.sort("bucket")
    b = rows["bucket"].to_numpy()
    lo, hi = int(b.min()), int(b.max())
    buckets = np.arange(lo, hi + 1, dtype=np.int64)
    idx = b - lo

    def fill(col: str, dtype: type) -> np.ndarray:
        out: np.ndarray = np.zeros(len(buckets), dtype=dtype)
        out[idx] = rows[col].to_numpy()
        return out

    return Ladder(
        bar_ts=bar_ts,
        buckets=buckets,
        ask=fill("ask_vol", np.float64),
        bid=fill("bid_vol", np.float64),
        ask_trades=fill("ask_trades", np.int64),
        bid_trades=fill("bid_trades", np.int64),
    )


def imbalances(lad: Ladder, ratio: float, min_volume: float) -> tuple[np.ndarray, np.ndarray]:
    """Boolean arrays (buy_imb, sell_imb) aligned with lad.buckets."""
    n = len(lad.buckets)
    buy = np.zeros(n, dtype=bool)
    sell = np.zeros(n, dtype=bool)
    if n >= 2:
        ask_hi, bid_lo = lad.ask[1:], lad.bid[:-1]  # ask[p] vs bid[p-1]
        buy[1:] = (ask_hi > 0) & (ask_hi >= min_volume) & (ask_hi >= ratio * bid_lo)
        bid_l, ask_h = lad.bid[:-1], lad.ask[1:]  # bid[p] vs ask[p+1]
        sell[:-1] = (bid_l > 0) & (bid_l >= min_volume) & (bid_l >= ratio * ask_h)
    return buy, sell


def stacked_runs(flags: np.ndarray, stack_n: int) -> list[tuple[int, int]]:
    """Index ranges [start, end] (inclusive) of runs of True with length >= stack_n."""
    runs: list[tuple[int, int]] = []
    start = None
    for i, f in enumerate([*flags.tolist(), False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            if i - start >= stack_n:
                runs.append((start, i - 1))
            start = None
    return runs


@dataclass(frozen=True, slots=True)
class BarSummary:
    bar_ts: int
    poc_bucket: int
    delta: float
    volume: float
    buy_imb_buckets: list[int]
    sell_imb_buckets: list[int]
    stacked_buy: list[tuple[int, int]]  # (low_bucket, high_bucket)
    stacked_sell: list[tuple[int, int]]


def summarize_bar(lad: Ladder, ratio: float, min_volume: float, stack_n: int) -> BarSummary:
    buy, sell = imbalances(lad, ratio, min_volume)
    b = lad.buckets
    return BarSummary(
        bar_ts=lad.bar_ts,
        poc_bucket=lad.poc_bucket(),
        delta=lad.delta,
        volume=float(lad.total.sum()),
        buy_imb_buckets=[int(x) for x in b[buy]],
        sell_imb_buckets=[int(x) for x in b[sell]],
        stacked_buy=[(int(b[s]), int(b[e])) for s, e in stacked_runs(buy, stack_n)],
        stacked_sell=[(int(b[s]), int(b[e])) for s, e in stacked_runs(sell, stack_n)],
    )
