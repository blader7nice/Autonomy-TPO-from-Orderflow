"""1m kline feed for the paper engine: research kline store + live store + REST.

History comes from the research kline store (`data/klines`) and the live store
(`data/paper/klines/<SYMBOL>/<YYYY-MM>.parquet`, minutes fetched by the engine). New minutes
come from Binance REST `/fapi/v1/klines`. The newest REST row is the forming candle and is
never used: only rows followed by a later row are complete (exchange data only, no clock).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from flowlevels.data import klines

log = logging.getLogger(__name__)
MIN = 60_000
SCHEMA = {
    "minute_ts": pl.Int64,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
}

Fetch = Callable[[str, int | None, int], Awaitable[pl.DataFrame]]


def month_key(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts / 1000, dt.UTC).strftime("%Y-%m")


def complete_rows(df: pl.DataFrame) -> pl.DataFrame:
    """Drop the last (forming) row of a response that reaches the present."""
    return df.head(max(df.height - 1, 0))


@dataclass
class SymbolFeed:
    symbol: str
    df: pl.DataFrame
    store_max: int  # last minute held by the research store (live store holds later ones)
    dirty_months: set[str] = field(default_factory=set)

    @property
    def last_ts(self) -> int | None:
        return int(self.df["minute_ts"][-1]) if self.df.height else None

    def append(self, new: pl.DataFrame) -> pl.DataFrame:
        """Append minutes later than the last one; returns what was added."""
        last = self.last_ts
        add = new if last is None else new.filter(pl.col("minute_ts") > last)
        if add.height:
            add = add.unique("minute_ts").sort("minute_ts").cast(SCHEMA)  # type: ignore[arg-type]
            self.df = pl.concat([self.df, add])
            self.dirty_months |= {month_key(int(t)) for t in add["minute_ts"].to_list()}
        return add

    def missing_minutes(self, since: int) -> int:
        ts = self.df.filter(pl.col("minute_ts") >= since)["minute_ts"]
        if ts.len() < 2:
            return 0
        return int((int(ts[-1]) - int(ts[0])) // MIN + 1 - ts.len())


class KlineFeed:
    def __init__(
        self,
        research_root: Path,
        live_root: Path,
        fetch: Fetch,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        page: int = 1000,
        pace_s: float = 1.0,
    ) -> None:
        self.research_root = research_root
        self.live_root = live_root
        self.fetch = fetch
        self.sleep = sleep
        self.page = page
        self.pace_s = pace_s
        self.frontier = 0  # open time of the newest forming minute seen (exchange time)
        self.feeds: dict[str, SymbolFeed] = {}

    # ------------------------------------------------------------------ history
    def load(self, symbol: str, anchor: int) -> SymbolFeed:
        res = klines.load(self.research_root, symbol, anchor, 2**62)
        live = klines.load(self.live_root, symbol, anchor, 2**62)
        store_max = int(res["minute_ts"].max()) if res.height else anchor - MIN  # type: ignore[arg-type]
        df = (
            pl.concat([res.cast(SCHEMA), live.cast(SCHEMA)])  # type: ignore[arg-type]
            .unique("minute_ts", keep="first")
            .sort("minute_ts")
        )
        f = SymbolFeed(symbol, df, store_max)
        self.feeds[symbol] = f
        return f

    def flush(self) -> int:
        """Write the dirty months of the live store (minutes after the research store)."""
        n = 0
        for f in self.feeds.values():
            for mk in sorted(f.dirty_months):
                y, mo = (int(x) for x in mk.split("-"))
                lo = _month_start(y, mo)
                hi = _month_start(y + (mo == 12), mo % 12 + 1)
                part = f.df.filter(
                    (pl.col("minute_ts") >= max(lo, f.store_max + MIN)) & (pl.col("minute_ts") < hi)
                )
                if not part.height:
                    continue
                d = self.live_root / f.symbol
                d.mkdir(parents=True, exist_ok=True)
                tmp = d / f".{mk}.parquet.tmp"
                part.write_parquet(tmp)
                tmp.replace(d / f"{mk}.parquet")
                n += 1
            f.dirty_months.clear()
        return n

    # ------------------------------------------------------------------ REST
    async def backfill(self, symbol: str) -> int:
        """Fetch every closed minute after the last stored one (pages of `page`)."""
        f = self.feeds[symbol]
        got: list[pl.DataFrame] = []
        start = (f.last_ts or 0) + MIN
        while True:
            df = await self.fetch(symbol, start, self.page)
            got.append(df)
            if df.height < self.page:
                break
            start = int(df["minute_ts"][-1]) + MIN
            await self.sleep(self.pace_s)
        allrows = pl.concat(got) if got else pl.DataFrame(schema=SCHEMA)
        return f.append(complete_rows(allrows.unique("minute_ts").sort("minute_ts"))).height

    async def poll(self, symbol: str, limit: int) -> int:
        """Latest closed minutes; falls back to a backfill when there is a gap. The newest row
        of a response is always dropped: it is the forming candle, or, when the REST snapshot
        is a few seconds stale, a closed minute that may still miss its last trades."""
        f = self.feeds[symbol]
        raw = await self.fetch(symbol, None, limit)
        if raw.height:  # exchange-time evidence: the newest (forming) minute any symbol shows
            self.frontier = max(self.frontier, int(raw["minute_ts"][-1]))
        df = complete_rows(raw)
        if not df.height:
            return 0
        last = f.last_ts
        if last is not None and int(df["minute_ts"][0]) > last + MIN:
            return await self.backfill(symbol)
        return f.append(df).height

    def lagging(self, symbols: list[str], clock_minute: int = 0) -> list[str]:
        """Symbols whose last closed minute is behind the minute before the frontier.
        `clock_minute` (wall clock, ops only) catches a snapshot that is stale for every
        symbol at once; it only decides whether to poll again, never which rows are used."""
        want = max(self.frontier, clock_minute) - MIN
        return [s for s in symbols if (self.feeds[s].last_ts or 0) < want]


def _month_start(y: int, mo: int) -> int:
    return int(dt.datetime(y, mo, 1, tzinfo=dt.UTC).timestamp() * 1000)
