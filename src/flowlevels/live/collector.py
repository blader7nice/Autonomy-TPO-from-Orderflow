"""Live collector: LiveSource -> Ingestor -> hourly parquet (raw ticks, 1m footprint/OHLCV,
liquidations), with a JSON event log (connects, gaps, backfills, watchdog).

Phase 1 scope: data collection only. Levels/signals/alerts are added in Phase 5.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

from flowlevels.config import AppConfig
from flowlevels.data.ingest import HourlyParquetSink, Ingestor
from flowlevels.data.storage import ParquetStore
from flowlevels.stream.source import LiveConfig, LiveEvent, LiveSource
from flowlevels.venues.base import VenueAdapter

log = logging.getLogger(__name__)


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, ev: LiveEvent) -> None:
        rec = {
            "wall_utc": dt.datetime.fromtimestamp(ev.wall_ts, dt.UTC).isoformat(),
            "kind": ev.kind,
            **ev.detail,
        }
        with self.path.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")


def resume_ids(store: ParquetStore, symbols: list[str]) -> dict[str, int]:
    """Last stored trade id per symbol (restart safety: backfill from there)."""
    out: dict[str, int] = {}
    for s in symbols:
        days = store.days("ohlcv_1m", s)
        for day in reversed(days):
            try:
                df = store.read("ohlcv_1m", s, day, day)
            except FileNotFoundError:
                continue
            if df.height:
                out[s] = int(df["last_id"].max())  # type: ignore[arg-type]
                break
    return out


async def run_collector(
    adapter: VenueAdapter,
    cfg: AppConfig,
    store: ParquetStore,
    symbols: list[str],
    duration_s: float | None,
    live_cfg: LiveConfig | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    ev_path = store.data_dir / "logs" / f"live-events-{dt.datetime.now(dt.UTC):%Y%m%d}.jsonl"
    source = LiveSource(
        adapter,
        symbols,
        cfg=live_cfg,
        resume_ids=resume_ids(store, symbols) if resume else None,
        on_event=EventLog(ev_path),
    )
    sink = HourlyParquetSink(store, session=f"{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S}")
    ing = Ingestor(cfg, symbols, [sink])
    # The ingestor's tracker must agree with the source's resume point.
    for s, t in source.trackers.items():
        ing.trackers[s].last_id = t.last_id
        ing.trackers[s].last_ts_ms = t.last_ts_ms

    task = asyncio.create_task(ing.run(source, flush_at_end=False))
    try:
        if duration_s is not None:
            await asyncio.sleep(duration_s)
            source.stop()
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        # The open minute stays unflushed (its trades are in raw ticks); a restart
        # rebuilds it after backfill. Finalized minutes and raw ticks are written now.
        sink.flush_all()
    gaps = {s: [g.to_dict() for g in t.gaps] for s, t in source.trackers.items()}
    return {
        "kind": "live_smoke",
        "symbols": symbols,
        "duration_s": duration_s,
        "trades": dict(ing.n_trades),
        "reconnects": source.reconnects,
        "gaps": gaps,
        "events": [{"kind": e.kind, **e.detail} for e in source.events],
        "event_log": str(ev_path),
    }
