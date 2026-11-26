"""Data-quality exclusions: bars and sessions affected by recorded data holes (D-026, D-031).

Holes are not repaired (owner decision 2026-11-26). Everything that computes statistics
(baselines, level quality checks, research) must exclude what a hole touches and report how
much it excluded. This module is the single place that decides "affected":

- a **bar** [bar_ts, bar_ts + res) is affected if it overlaps a hole;
- a **session** (UTC day) is affected for level statistics if any hole overlaps the data its
  levels are computed from: [history_start_ms(session open), session close). That covers the
  prior-day/-week profiles, naked POCs, the monthly VWAP and ATR.

A hole is (after_ms, before_ms): the last trade before and the first trade after a silence
longer than `data_quality.max_trade_silence_s`; the missing data lies strictly between them.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import polars as pl

from flowlevels.config import LevelsConfig
from flowlevels.data.storage import ParquetStore, day_start_ms
from flowlevels.levels.engine import DAY_MS, history_start_ms


@dataclass(frozen=True, slots=True)
class Hole:
    symbol: str
    after_ms: int
    before_ms: int

    def overlaps(self, start_ms: int, end_ms: int) -> bool:
        """True if the open interval (after, before) intersects [start, end)."""
        return self.after_ms < end_ms and self.before_ms > start_ms


def load_holes(store: ParquetStore, symbol: str) -> list[Hole]:
    """All holes recorded in the ohlcv_1m completion markers of `symbol`."""
    out = []
    for day in store.days("ohlcv_1m", symbol):
        info = store.completion("ohlcv_1m", symbol, day) or {}
        for h in info.get("holes", []):
            out.append(Hole(symbol, int(h["after_ms"]), int(h["before_ms"])))
    return sorted(out, key=lambda h: h.after_ms)


def overlaps_any(holes: list[Hole], start_ms: int, end_ms: int) -> bool:
    return any(h.overlaps(start_ms, end_ms) for h in holes)


def bar_mask(bar_ts: pl.Series, res_ms: int, holes: list[Hole]) -> pl.Series:
    """Boolean Series, True where the bar [ts, ts + res_ms) overlaps a hole (exclude it)."""
    expr = pl.lit(False)
    for h in holes:
        expr = expr | ((pl.col("t") < h.before_ms) & (pl.col("t") + res_ms > h.after_ms))
    return pl.DataFrame({"t": bar_ts}).select(expr.alias("in_hole"))["in_hole"]


def level_session_window(day: dt.date, lc: LevelsConfig) -> tuple[int, int]:
    """[start, end) of the data that levels during session `day` depend on."""
    d0 = day_start_ms(day)
    return history_start_ms(d0, lc), d0 + DAY_MS


@dataclass
class Exclusions:
    """What a report excluded because of data holes. Every report prints `summary()`."""

    holes: list[Hole]
    sessions_total: int = 0
    excluded: list[tuple[str, dt.date]] = field(default_factory=list)

    @property
    def hole_minutes(self) -> float:
        return sum((h.before_ms - h.after_ms) / 60_000 for h in self.holes)

    def summary(self) -> str:
        n = len(self.excluded)
        pct = n / self.sessions_total if self.sessions_total else 0.0
        holes = (
            "; ".join(f"{h.symbol} {_fmt(h.after_ms)}–{_fmt(h.before_ms)}" for h in self.holes)
            or "none"
        )
        days = ", ".join(f"{s} {d}" for s, d in self.excluded) or "none"
        return (
            f"Data holes (not repaired, D-026/D-031): {holes} "
            f"({self.hole_minutes:.1f} min in total). "
            f"Excluded sessions (level inputs overlap a hole): {n} of {self.sessions_total} "
            f"({pct:.1%}): {days}."
        )


def _fmt(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC).strftime("%Y-%m-%d %H:%M:%S")


def clean_sessions(
    store: ParquetStore,
    symbol: str,
    days: list[dt.date],
    lc: LevelsConfig,
    excl: Exclusions | None = None,
) -> list[dt.date]:
    """`days` minus sessions whose level inputs overlap a hole; records them in `excl`."""
    holes = load_holes(store, symbol)
    out = []
    for d in days:
        if overlaps_any(holes, *level_session_window(d, lc)):
            if excl is not None:
                excl.excluded.append((symbol, d))
        else:
            out.append(d)
    if excl is not None:
        excl.sessions_total += len(days)
        excl.holes.extend(h for h in holes if h not in excl.holes)
    return out
