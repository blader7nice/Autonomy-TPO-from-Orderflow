"""Anchored VWAP with volume-weighted sigma bands, from exact running sums.

State per anchor = (sum_pv, sum_v, sum_p2v). VWAP = sum_pv / sum_v,
sigma = sqrt(max(sum_p2v / sum_v - VWAP^2, 0)). The same accumulator serves history and live
(add 1m rows as they finalize), so weekly/monthly VWAP never needs the raw history.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

import polars as pl


@dataclass
class VwapAccumulator:
    anchor_ms: int
    sum_pv: float = 0.0
    sum_v: float = 0.0
    sum_p2v: float = 0.0

    def add_rows(self, ohlcv_1m: pl.DataFrame) -> None:
        if ohlcv_1m.height:
            # math.fsum is exactly rounded, hence independent of row order / threading:
            # identical inputs give bit-identical levels (needed for replay/live parity).
            self.sum_pv = math.fsum([self.sum_pv, *ohlcv_1m["sum_pv"].to_list()])
            self.sum_v = math.fsum([self.sum_v, *ohlcv_1m["volume"].to_list()])
            self.sum_p2v = math.fsum([self.sum_p2v, *ohlcv_1m["sum_p2v"].to_list()])

    @property
    def vwap(self) -> float | None:
        return self.sum_pv / self.sum_v if self.sum_v > 0 else None

    @property
    def sigma(self) -> float | None:
        v = self.vwap
        if v is None:
            return None
        return math.sqrt(max(self.sum_p2v / self.sum_v - v * v, 0.0))

    def bands(self) -> dict[str, float]:
        v, s = self.vwap, self.sigma
        if v is None or s is None:
            return {}
        return {"": v, "+1s": v + s, "-1s": v - s, "+2s": v + 2 * s, "-2s": v - 2 * s}


def _ms(d: dt.date) -> int:
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.UTC).timestamp() * 1000)


def anchor_ms(kind: str, t_ms: int) -> int:
    """Start of the day / ISO week (Monday) / month containing t (UTC)."""
    d = dt.datetime.fromtimestamp(t_ms / 1000, tz=dt.UTC).date()
    if kind == "d":
        return _ms(d)
    if kind == "w":
        return _ms(d - dt.timedelta(days=d.weekday()))
    if kind == "m":
        return _ms(d.replace(day=1))
    raise ValueError(kind)


def anchored_vwap(ohlcv_closed: pl.DataFrame, kind: str, t_ms: int) -> VwapAccumulator:
    """VWAP anchored at the start of the period containing t, over 1m rows closed by t."""
    acc = VwapAccumulator(anchor_ms(kind, t_ms))
    acc.add_rows(ohlcv_closed.filter(pl.col("minute_ts") >= acc.anchor_ms))
    return acc
