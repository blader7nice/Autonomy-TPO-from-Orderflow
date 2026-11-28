"""Streaming historical pipeline: one day at a time.

download -> verify checksum -> parse -> Ingestor (same code path as live) -> write 1m
footprint + 1m OHLCV -> mark complete -> delete raw.
"""

from __future__ import annotations

import datetime as dt
import itertools
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from flowlevels.config import AppConfig
from flowlevels.data.ingest import Ingestor, MemorySink
from flowlevels.data.repair import merge_minutes, repair_day_holes
from flowlevels.data.storage import ParquetStore, day_start_ms
from flowlevels.stream.source import ReplaySource
from flowlevels.stream.types import TRADE_SCHEMA
from flowlevels.venues.base import DataNotAvailableError, VenueAdapter

log = logging.getLogger(__name__)

FOOTPRINT_FORMAT_VERSION = 3  # 2: records time holes (D-026); 3: hole repair (D-032)


@dataclass
class DayReport:
    symbol: str
    day: str
    status: str  # done | skipped | missing | error
    n_trades: int = 0
    first_id: int | None = None
    last_id: int | None = None
    gaps: list[dict[str, Any]] = field(default_factory=list)
    holes: list[dict[str, Any]] = field(default_factory=list)  # unrepaired silences (D-026)
    repairs: list[dict[str, Any]] = field(default_factory=list)  # repaired holes (D-032)
    raw_bytes: int = 0
    out_bytes: int = 0
    seconds: float = 0.0
    download_s: float = 0.0  # download + checksum verification
    process_s: float = 0.0  # parse + build + write
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def fingerprint(cfg: AppConfig, symbol: str) -> dict[str, Any]:
    """What the stored derived data depends on; a mismatch forces a rebuild."""
    sc = cfg.symbol(symbol)
    return {
        "format": FOOTPRINT_FORMAT_VERSION,
        "exchange_tick": sc.exchange_tick,
        "bucket": sc.bucket,
        "max_trade_silence_s": cfg.data_quality.max_trade_silence_s,
        "repair_holes": cfg.data_quality.repair_holes_from_trades,
    }


def find_time_holes(
    trades: pl.DataFrame, start_ms: int, end_ms: int, max_silence_s: float
) -> list[dict[str, Any]]:
    """Stretches inside [start_ms, end_ms) with no trade for longer than max_silence_s.

    Id-gap detection cannot see a hole when the venue never issued ids for the missing
    period (D-026), so the time axis is checked too. The day edges count as boundaries.
    """
    limit = int(max_silence_s * 1000)
    pts = pl.concat(
        [
            pl.Series([start_ms - 1], dtype=pl.Int64),
            trades["ts_ms"].cast(pl.Int64),
            pl.Series([end_ms], dtype=pl.Int64),
        ]
    )
    out: list[dict[str, Any]] = []
    for i in (pts.diff() > limit).arg_true().to_list():
        a, b = int(pts[i - 1]), int(pts[i])
        out.append({"after_ms": a, "before_ms": b, "seconds": round((b - a) / 1000, 3)})
    return out


class _StreamStats:
    """Per-day checks computed batch by batch: trade count, first/last id, trades outside
    the day (error) and time holes (same result as `find_time_holes` on the whole day)."""

    def __init__(self, start: int, end: int, max_silence_s: float) -> None:
        self.start, self.end = start, end
        self.limit = int(max_silence_s * 1000)
        self.n = 0
        self.first_id: int | None = None
        self.last_id: int | None = None
        self.prev_ts = start - 1
        self._holes: list[dict[str, Any]] = []

    def see(self, name: str, day: dt.date, trades: pl.DataFrame) -> pl.DataFrame:
        if trades.height == 0:
            return trades
        outside = trades.filter((pl.col("ts_ms") < self.start) | (pl.col("ts_ms") >= self.end))
        if outside.height:
            raise ValueError(f"{name}: {outside.height} trades outside {day} (UTC)")
        self.n += trades.height
        if self.first_id is None:
            self.first_id = int(trades["trade_id"][0])
        self.last_id = int(trades["trade_id"][-1])
        self._scan(trades["ts_ms"].cast(pl.Int64))
        return trades

    def _scan(self, ts: pl.Series) -> None:
        pts = pl.concat([pl.Series([self.prev_ts], dtype=pl.Int64), ts])
        for i in (pts.diff() > self.limit).arg_true().to_list():
            a, b = int(pts[i - 1]), int(pts[i])
            self._holes.append({"after_ms": a, "before_ms": b, "seconds": round((b - a) / 1000, 3)})
        self.prev_ts = int(ts[-1])

    def holes(self) -> list[dict[str, Any]]:
        self._scan(pl.Series([self.end], dtype=pl.Int64))
        return self._holes


async def process_day(
    adapter: VenueAdapter,
    store: ParquetStore,
    cfg: AppConfig,
    symbol: str,
    day: dt.date,
    raw_dir: Path,
    keep_raw: bool = False,
    force: bool = False,
    chunk_rows: int | None = None,
) -> DayReport:
    """One day. With `chunk_rows` the archive is streamed in batches (bounded memory, the
    same Ingestor code path and identical output); only a day with a time hole is re-read
    in full for the repair step, which needs the fill-id ranges of all trades."""
    t0 = time.perf_counter()
    fp = fingerprint(cfg, symbol)
    done = store.completion("ohlcv_1m", symbol, day)
    if not force and done is not None and done.get("fingerprint") == fp:
        rep = DayReport(symbol, day.isoformat(), "skipped")
        rep.n_trades = int(done.get("n_trades", 0))
        rep.first_id, rep.last_id = done.get("first_id"), done.get("last_id")
        rep.gaps = list(done.get("gaps", []))
        rep.holes = list(done.get("holes", []))
        rep.repairs = list(done.get("repairs", []))
        return rep
    try:
        path = await adapter.download_trades_day(symbol, day, raw_dir)
    except DataNotAvailableError as exc:
        return DayReport(symbol, day.isoformat(), "missing", error=str(exc))
    raw_bytes = path.stat().st_size
    t_dl = time.perf_counter() - t0
    start = day_start_ms(day)
    end = start + 86_400_000
    sink = MemorySink()
    ing = Ingestor(cfg, [symbol], [sink])
    stats = _StreamStats(start, end, cfg.data_quality.max_trade_silence_s)
    agg: pl.DataFrame | None = None
    if chunk_rows:
        frames = (stats.see(path.name, day, f) for f in adapter.iter_trades_file(path, chunk_rows))
        await ing.run(ReplaySource.from_frames(symbol, frames))
    else:
        agg = adapter.read_trades_file(path, keep_fill_ids=True)
        trades = stats.see(path.name, day, agg.select(list(TRADE_SCHEMA)))
        await ing.run(ReplaySource.from_frames(symbol, [trades]))
    fp_df, ohlcv_df = sink.footprint_df(symbol), sink.ohlcv_df(symbol)
    gaps = [g.to_dict() for g in ing.trackers[symbol].gaps]
    holes = stats.holes()
    repairs: list[dict[str, Any]] = []
    if holes and cfg.data_quality.repair_holes_from_trades and agg is None:
        agg = adapter.read_trades_file(path, keep_fill_ids=True)  # rare: hole days only
    if holes:
        log.warning("%s %s: %d time hole(s) without trades: %s", symbol, day, len(holes), holes)
    if holes and cfg.data_quality.repair_holes_from_trades:
        assert agg is not None
        trades = agg.select(list(TRADE_SCHEMA))
        fix = await repair_day_holes(adapter, symbol, day, agg, holes, raw_dir, keep_raw)
        if fix.fills.height:
            # The fills go through their own Ingestor: same code path, own id namespace.
            fsink = MemorySink()
            fing = Ingestor(cfg, [symbol], [fsink])
            await fing.run(ReplaySource.from_frames(symbol, [fix.fills]))
            fp_df, ohlcv_df = merge_minutes(
                fp_df,
                ohlcv_df,
                trades,
                fsink.footprint_df(symbol),
                fsink.ohlcv_df(symbol),
                fix.fills,
            )
        repairs, holes = fix.repaired, fix.unrepaired
        log.warning(
            "%s %s: repaired %d hole(s) from the raw trades archive, %d left",
            symbol,
            day,
            len(repairs),
            len(holes),
        )
    del agg
    # A complete archive day supersedes partial live parts of the same day (D-032).
    for ds in ("footprint_1m", "ohlcv_1m"):
        store.remove_parts(ds, symbol, day, keep="hist")
    p1 = store.write_part("footprint_1m", symbol, day, "hist", fp_df)
    p2 = store.write_part("ohlcv_1m", symbol, day, "hist", ohlcv_df)
    rep = DayReport(
        symbol=symbol,
        day=day.isoformat(),
        status="done",
        n_trades=stats.n,
        first_id=stats.first_id,
        last_id=stats.last_id,
        gaps=gaps,
        holes=holes,
        repairs=repairs,
        raw_bytes=raw_bytes,
        out_bytes=p1.stat().st_size + p2.stat().st_size,
    )
    info = {
        "fingerprint": fp,
        "source": path.name,
        "n_trades": rep.n_trades,
        "first_id": rep.first_id,
        "last_id": rep.last_id,
        "gaps": gaps,
        "holes": holes,  # unrepaired: excluded by flowlevels.data.quality (D-031)
        "repairs": repairs,  # repaired, with source file + checksum and kline checks (D-032)
        "minutes": ohlcv_df.height,
    }
    store.mark_complete("footprint_1m", symbol, day, info)
    store.mark_complete("ohlcv_1m", symbol, day, info)
    if not keep_raw:
        path.unlink(missing_ok=True)
    rep.seconds = round(time.perf_counter() - t0, 3)
    rep.download_s = round(t_dl, 3)
    rep.process_s = round(rep.seconds - t_dl, 3)
    return rep


def last_complete_days(n: int, today: dt.date | None = None) -> list[dt.date]:
    """The n UTC days before `today` (daily archives are published after the day ends)."""
    today = today or dt.datetime.now(dt.UTC).date()
    return [today - dt.timedelta(days=i) for i in range(n, 0, -1)]


def cross_day_gaps(reports: list[DayReport]) -> list[dict[str, Any]]:
    """Id discontinuities between consecutive available days of the same symbol."""
    out: list[dict[str, Any]] = []
    by_sym: dict[str, list[DayReport]] = {}
    for r in reports:
        if r.first_id is not None:
            by_sym.setdefault(r.symbol, []).append(r)
    for sym, rs in by_sym.items():
        rs.sort(key=lambda r: r.day)
        for a, b in itertools.pairwise(rs):
            assert a.last_id is not None and b.first_id is not None
            if b.first_id != a.last_id + 1:
                out.append(
                    {
                        "symbol": sym,
                        "after_day": a.day,
                        "before_day": b.day,
                        "after_id": a.last_id,
                        "before_id": b.first_id,
                        "missing": b.first_id - a.last_id - 1,
                    }
                )
    return out
