"""Candle store: Binance USD-M 1m klines per symbol and month (candles only, no orderflow).

Layout: `<data_dir>/klines/<SYMBOL>/<YYYY-MM>.parquet` with columns minute_ts (open time,
ms), open, high, low, close, volume (base asset). Complete past months come from the monthly
archive; the current month from daily files up to a given last day (a partial month file is
named `<YYYY-MM>.partial-<DD>.parquet` and replaced when a later fill runs). Downloads are
sha256-verified by the adapter; the raw zip is deleted after parsing.
"""

from __future__ import annotations

import datetime as dt
import logging
import shutil
from pathlib import Path

import polars as pl

from flowlevels.venues.base import VenueAdapter

log = logging.getLogger(__name__)
COLS = ["minute_ts", "open", "high", "low", "close", "volume"]


def _norm(df: pl.DataFrame) -> pl.DataFrame:
    return (
        df.rename({"open_time": "minute_ts"})
        .select(COLS)
        .unique("minute_ts", keep="first")
        .sort("minute_ts")
    )


def month_files(root: Path, symbol: str) -> list[Path]:
    return sorted((root / symbol).glob("*.parquet"))


async def fill_month(
    adapter: VenueAdapter, root: Path, symbol: str, year: int, month: int, last_day: dt.date
) -> Path | None:
    """Ensure the month is stored. Returns the parquet path, or None if no data exists."""
    d = root / symbol
    d.mkdir(parents=True, exist_ok=True)
    full = d / f"{year:04d}-{month:02d}.parquet"
    if full.exists():
        return full
    first = dt.date(year, month, 1)
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    tmp = d / "_raw"
    tmp.mkdir(exist_ok=True)
    try:
        if nxt - dt.timedelta(days=1) <= last_day:
            try:
                z = await adapter.download_klines_1m_month(symbol, year, month, tmp)
            except Exception as exc:  # 404 for symbols not listed that month
                log.warning("no monthly klines %s %04d-%02d: %s", symbol, year, month, exc)
                return None
            _norm(adapter.read_klines_file(z)).write_parquet(full)
            return full
        for p in d.glob(f"{year:04d}-{month:02d}.partial-*.parquet"):
            p.unlink()
        parts = []
        day = first
        while day <= last_day:
            try:
                z = await adapter.download_klines_1m_day(symbol, day, tmp)
                parts.append(_norm(adapter.read_klines_file(z)))
                z.unlink()
            except Exception as exc:
                log.warning("no daily klines %s %s: %s", symbol, day, exc)
            day += dt.timedelta(days=1)
        if not parts:
            return None
        out = d / f"{year:04d}-{month:02d}.partial-{last_day.day:02d}.parquet"
        pl.concat(parts).unique("minute_ts").sort("minute_ts").write_parquet(out)
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def fill_range(
    adapter: VenueAdapter, root: Path, symbol: str, start: dt.date, end: dt.date
) -> list[Path]:
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        p = await fill_month(adapter, root, symbol, y, m, end)
        if p is not None:
            out.append(p)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def load(root: Path, symbol: str, start_ms: int, end_ms: int) -> pl.DataFrame:
    """1m candles with start_ms <= minute_ts < end_ms (sorted, unique)."""
    files = month_files(root, symbol)
    if not files:
        return pl.DataFrame(schema={c: pl.Int64 if c == "minute_ts" else pl.Float64 for c in COLS})
    lf = pl.scan_parquet(files).filter(
        (pl.col("minute_ts") >= start_ms) & (pl.col("minute_ts") < end_ms)
    )
    return lf.collect().unique("minute_ts").sort("minute_ts")
