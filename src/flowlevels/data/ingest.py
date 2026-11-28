"""Ingestor: TradeSource -> 1m footprint/OHLCV (+ raw ticks, liquidations) via sinks.

This is the one code path shared by historical replay and live (hard rule 1).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Protocol

import polars as pl

from flowlevels.config import AppConfig
from flowlevels.data.storage import ParquetStore
from flowlevels.footprint.minute import FinalizedMinutes, MinuteBuilder
from flowlevels.stream.gaps import GapTracker
from flowlevels.stream.source import TradeSource
from flowlevels.stream.types import validate_trades

HOUR_MS = 3_600_000


class Sink(Protocol):
    def on_trades(self, symbol: str, trades: pl.DataFrame) -> None: ...
    def on_minutes(self, symbol: str, minutes: FinalizedMinutes) -> None: ...
    def on_liquidations(self, symbol: str, liqs: pl.DataFrame) -> None: ...


class Ingestor:
    def __init__(self, cfg: AppConfig, symbols: list[str], sinks: list[Sink]) -> None:
        self.cfg = cfg
        self.sinks = sinks
        self.builders = {
            s: MinuteBuilder(cfg.symbol(s).exchange_tick, cfg.symbol(s).bucket) for s in symbols
        }
        self.trackers = {s: GapTracker(s) for s in symbols}
        self.n_trades: dict[str, int] = defaultdict(int)

    def push_trades(self, symbol: str, trades: pl.DataFrame) -> None:
        if symbol not in self.builders or trades.height == 0:
            return
        validate_trades(trades)
        trades = self.trackers[symbol].drop_seen(trades)
        if trades.height == 0:
            return
        self.trackers[symbol].observe(trades)
        self.n_trades[symbol] += trades.height
        for s in self.sinks:
            s.on_trades(symbol, trades)
        done = self.builders[symbol].push(trades)
        if not done.empty:
            for s in self.sinks:
                s.on_minutes(symbol, done)

    def push_liquidations(self, symbol: str, liqs: pl.DataFrame) -> None:
        for s in self.sinks:
            s.on_liquidations(symbol, liqs)

    def flush(self) -> None:
        for symbol, b in self.builders.items():
            done = b.flush()
            if not done.empty:
                for s in self.sinks:
                    s.on_minutes(symbol, done)

    async def run(self, source: TradeSource, flush_at_end: bool = True) -> None:
        async for batch in source:
            if batch.kind == "trades":
                self.push_trades(batch.symbol, batch.df)
            else:
                self.push_liquidations(batch.symbol, batch.df)
        if flush_at_end:
            self.flush()


class MemorySink:
    """Collects everything in memory (historical day processing, tests)."""

    def __init__(self, keep_trades: bool = False) -> None:
        self.keep_trades = keep_trades
        self.trades: dict[str, list[pl.DataFrame]] = defaultdict(list)
        self.footprint: dict[str, list[pl.DataFrame]] = defaultdict(list)
        self.ohlcv: dict[str, list[pl.DataFrame]] = defaultdict(list)
        self.liquidations: dict[str, list[pl.DataFrame]] = defaultdict(list)

    def on_trades(self, symbol: str, trades: pl.DataFrame) -> None:
        if self.keep_trades:
            self.trades[symbol].append(trades)

    def on_minutes(self, symbol: str, minutes: FinalizedMinutes) -> None:
        self.footprint[symbol].append(minutes.footprint)
        self.ohlcv[symbol].append(minutes.ohlcv)

    def on_liquidations(self, symbol: str, liqs: pl.DataFrame) -> None:
        self.liquidations[symbol].append(liqs)

    @staticmethod
    def _cat(frames: list[pl.DataFrame]) -> pl.DataFrame:
        return pl.concat(frames, how="vertical") if frames else pl.DataFrame()

    def footprint_df(self, symbol: str) -> pl.DataFrame:
        return self._cat(self.footprint[symbol])

    def ohlcv_df(self, symbol: str) -> pl.DataFrame:
        return self._cat(self.ohlcv[symbol])


class HourlyParquetSink:
    """Live sink: buffers per UTC hour and writes idempotent hourly parquet parts.

    Each flush rewrites the part for the hour with everything buffered for that hour, so a
    crash loses at most the unflushed tail and a restart never duplicates rows.
    """

    def __init__(self, store: ParquetStore, session: str, write_raw: bool = True) -> None:
        # `session` makes part names unique per process run, so a restart never
        # overwrites the parts written before it (raw-tick overlap after backfill is
        # removed by de-duplicating on trade_id when reading).
        self.store = store
        self.session = session
        self.write_raw = write_raw
        # (dataset, symbol, hour_start_ms) -> frames
        self._buf: dict[tuple[str, str, int], list[pl.DataFrame]] = defaultdict(list)
        self._ts_col = {
            "raw_aggtrades": "ts_ms",
            "raw_liquidations": "ts_ms",
            "footprint_1m": "minute_ts",
            "ohlcv_1m": "minute_ts",
        }

    def _add(self, dataset: str, symbol: str, df: pl.DataFrame) -> None:
        if df.height == 0:
            return
        col = self._ts_col[dataset]
        hours = (df[col] // HOUR_MS).unique().sort().to_list()
        for h in hours:
            self._buf[(dataset, symbol, int(h) * HOUR_MS)].append(
                df.filter(pl.col(col) // HOUR_MS == h)
            )
        # Flush hours older than the newest buffered hour for this dataset/symbol.
        newest = max(k[2] for k in self._buf if k[0] == dataset and k[1] == symbol)
        for key in [k for k in self._buf if k[:2] == (dataset, symbol) and k[2] < newest]:
            self._write(key)
            del self._buf[key]

    def _write(self, key: tuple[str, str, int]) -> None:
        dataset, symbol, hour_ms = key
        df = pl.concat(self._buf[key], how="vertical")
        self._buf[key] = [df]
        hour = pl.Series([hour_ms]).cast(pl.Datetime("ms", "UTC"))[0]
        part = f"live-{hour:%H}-{self.session}"
        self.store.write_split_by_day(dataset, symbol, part, df, self._ts_col[dataset])

    def on_trades(self, symbol: str, trades: pl.DataFrame) -> None:
        if self.write_raw:
            self._add("raw_aggtrades", symbol, trades)

    def on_minutes(self, symbol: str, minutes: FinalizedMinutes) -> None:
        self._add("footprint_1m", symbol, minutes.footprint)
        self._add("ohlcv_1m", symbol, minutes.ohlcv)

    def on_liquidations(self, symbol: str, liqs: pl.DataFrame) -> None:
        self._add("raw_liquidations", symbol, liqs)

    def flush_all(self) -> None:
        for key in list(self._buf):
            self._write(key)
