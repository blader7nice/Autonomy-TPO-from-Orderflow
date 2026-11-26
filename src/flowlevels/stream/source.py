"""TradeSource: the single interface through which all market data enters the system.

- ReplaySource: historical batches (files / frames) -> Batch stream.
- LiveSource: WebSocket -> Batch stream, with id-gap backfill via REST, reconnects with
  exponential backoff + jitter, proactive reconnect before the max connection lifetime,
  and a no-trade watchdog.

The local monotonic clock is used ONLY for operational concerns (watchdog, connection
lifetime, backoff). It never enters any market logic (hard rule 3).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

import polars as pl

from flowlevels.stream.gaps import Gap, GapTracker
from flowlevels.stream.types import Batch
from flowlevels.venues.base import RestLookbackExceededError, VenueAdapter

log = logging.getLogger(__name__)


class TradeSource(Protocol):
    def __aiter__(self) -> AsyncIterator[Batch]: ...


class ReplaySource:
    """Replays batches in order. `chunk_rows` re-splits trade batches (to emulate live)."""

    def __init__(self, batches: Iterable[Batch], chunk_rows: int | None = None) -> None:
        self._batches = batches
        self._chunk_rows = chunk_rows

    @classmethod
    def from_frames(
        cls, symbol: str, frames: Iterable[pl.DataFrame], chunk_rows: int | None = None
    ) -> ReplaySource:
        return cls((Batch(symbol, "trades", f) for f in frames), chunk_rows)

    async def __aiter__(self) -> AsyncIterator[Batch]:
        for b in self._batches:
            if self._chunk_rows and b.df.height > self._chunk_rows:
                for off in range(0, b.df.height, self._chunk_rows):
                    yield Batch(b.symbol, b.kind, b.df.slice(off, self._chunk_rows))
            else:
                yield b
            await asyncio.sleep(0)


class WatchdogTimeoutError(RuntimeError):
    pass


@dataclass
class LiveConfig:
    max_lifetime_s: float = 23 * 3600  # reconnect proactively before the 24h server limit
    watchdog_s: float = 60.0
    watchdog_symbol: str | None = "BTCUSDT"
    backoff_base_s: float = 1.0
    backoff_max_s: float = 60.0
    backfill_page: int = 1000
    backfill_max_ids: int = 5_000_000
    # aggTrades costs 20 weight per page; 1 page/s = 1200/min = half the 2400/min IP limit,
    # which is shared with other services on the host (the adapter also enforces a budget).
    backfill_pause_s: float = 1.0
    liquidations: bool = True


@dataclass
class LiveEvent:
    kind: str  # connect | disconnect | lifetime | watchdog | gap | backfill | backfill_failed
    wall_ts: float
    detail: dict[str, Any] = field(default_factory=dict)


class LiveSource:
    def __init__(
        self,
        adapter: VenueAdapter,
        symbols: list[str],
        cfg: LiveConfig | None = None,
        resume_ids: dict[str, int] | None = None,
        on_event: Callable[[LiveEvent], Awaitable[None] | None] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_connections: int | None = None,
    ) -> None:
        self.adapter = adapter
        self.symbols = symbols
        self.cfg = cfg or LiveConfig()
        if self.cfg.watchdog_symbol is not None and self.cfg.watchdog_symbol not in symbols:
            self.cfg = LiveConfig(**{**self.cfg.__dict__, "watchdog_symbol": None})
        self.trackers = {s: GapTracker(s) for s in symbols}
        for s, last in (resume_ids or {}).items():
            self.trackers[s].last_id = last
            self.trackers[s].last_ts_ms = 0
        self.events: list[LiveEvent] = []
        self._on_event = on_event
        self._sleep = sleep
        self._clock = clock
        self._max_connections = max_connections  # for tests / smoke runs
        self._stopped = False
        self.reconnects = 0

    def stop(self) -> None:
        self._stopped = True

    async def _emit(self, kind: str, **detail: Any) -> None:
        ev = LiveEvent(kind, time.time(), detail)
        self.events.append(ev)
        log.info("live %s %s", kind, detail)
        if self._on_event:
            res = self._on_event(ev)
            if res is not None:
                await res

    def _backoff(self, attempt: int) -> float:
        cap = min(self.cfg.backoff_max_s, self.cfg.backoff_base_s * 2**attempt)
        return float(random.uniform(cap / 2, cap))

    async def __aiter__(self) -> AsyncIterator[Batch]:
        url = self.adapter.live_url(self.symbols, self.cfg.liquidations)
        attempt = 0
        connections = 0
        while not self._stopped:
            if self._max_connections is not None and connections >= self._max_connections:
                return
            connections += 1
            await self._emit("connect", url=url, attempt=attempt)
            started = self._clock()
            last_wd = started
            reason = "server_closed"
            stream = self.adapter.live_messages(url)
            it = stream.__aiter__()
            try:
                while not self._stopped:
                    now = self._clock()
                    life_left = self.cfg.max_lifetime_s - (now - started)
                    wd_left = self.cfg.watchdog_s - (now - last_wd)
                    if life_left <= 0:
                        reason = "lifetime"
                        break
                    if wd_left <= 0:
                        raise WatchdogTimeoutError(f"no {self.cfg.watchdog_symbol} trades")
                    timeout = min(life_left, wd_left)
                    try:
                        raw = await asyncio.wait_for(it.__anext__(), timeout=timeout)
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        continue  # loop re-evaluates lifetime / watchdog
                    batch = self.adapter.parse_live_message(raw)
                    if batch is None:
                        continue
                    attempt = 0
                    if batch.kind == "liquidations":
                        yield batch
                        continue
                    if batch.symbol == self.cfg.watchdog_symbol or self.cfg.watchdog_symbol is None:
                        last_wd = self._clock()
                    for out in await self._handle_trades(batch):
                        yield out
            except WatchdogTimeoutError as exc:
                reason = "watchdog"
                await self._emit("watchdog", error=str(exc))
            except Exception as exc:  # network errors of any kind -> reconnect
                reason = f"error:{type(exc).__name__}"
                log.warning("live stream error: %r", exc)
            finally:
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:  # pragma: no cover - best effort
                        log.debug("error closing stream", exc_info=True)
            await self._emit("disconnect", reason=reason)
            if self._stopped:
                return
            self.reconnects += 1
            if reason == "lifetime":
                await self._emit("lifetime")
                continue  # immediate reconnect; the next message triggers gap backfill
            await self._sleep(self._backoff(attempt))
            attempt += 1

    async def _handle_trades(self, batch: Batch) -> list[Batch]:
        tracker = self.trackers.get(batch.symbol)
        if tracker is None:
            return []
        df = tracker.drop_seen(batch.df)
        if df.height == 0:
            return []
        out: list[Batch] = []
        gaps = tracker.find_gaps(df.head(1))
        if gaps:
            gap = gaps[0]
            await self._emit("gap", **gap.to_dict())
            filled, reason = await self._backfill(batch.symbol, gap)
            if filled is not None and filled.height:
                tracker.observe(filled)
                out.append(Batch(batch.symbol, "trades", filled))
            if filled is not None and tracker.last_id == gap.before_id - 1:
                tracker.record_resolved(gap, "rest_backfill")
                await self._emit("backfill", symbol=batch.symbol, n=filled.height)
            else:
                await self._emit("backfill_failed", reason=reason, **gap.to_dict())
        tracker.observe(df)  # records the gap as unresolved if backfill failed
        out.append(Batch(batch.symbol, "trades", df))
        return out

    async def _backfill(self, symbol: str, gap: Gap) -> tuple[pl.DataFrame | None, str]:
        """REST backfill of a gap. Returns (trades or None, failure reason or "")."""
        if gap.missing > self.cfg.backfill_max_ids:
            log.error("gap of %d ids exceeds backfill_max_ids", gap.missing)
            return None, "exceeds_backfill_max_ids"
        frames: list[pl.DataFrame] = []
        next_id = gap.after_id + 1
        try:
            while next_id < gap.before_id:
                page = await self.adapter.fetch_trades_from_id(
                    symbol, next_id, self.cfg.backfill_page
                )
                page = page.filter(pl.col("trade_id") < gap.before_id)
                if page.height == 0:
                    break
                frames.append(page)
                next_id = int(page["trade_id"][-1]) + 1
                if next_id < gap.before_id:
                    await self._sleep(self.cfg.backfill_pause_s)
        except RestLookbackExceededError as exc:
            # REST only serves the last 48 h; older ids must come from the daily archive
            # (`make bootstrap`) once it is published.
            log.error("backfill for %s is beyond the REST lookback: %s", symbol, exc)
            return (pl.concat(frames) if frames else None), "beyond_rest_lookback"
        except Exception as exc:
            log.error("backfill failed for %s: %r", symbol, exc)
            return (pl.concat(frames) if frames else None), f"error:{type(exc).__name__}"
        return (pl.concat(frames) if frames else None), ""
