import datetime as dt
from pathlib import Path

import polars as pl

from flowlevels.config import load_config
from flowlevels.data.ingest import HourlyParquetSink, Ingestor, MemorySink
from flowlevels.data.storage import ParquetStore
from flowlevels.live.collector import resume_ids, run_collector
from flowlevels.stream.gaps import GapTracker
from flowlevels.stream.source import LiveConfig, LiveSource, ReplaySource
from tests.helpers import FakeAdapter, synthetic_trades, ws_agg_frame

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "configs/default.yaml")
TAPE = synthetic_trades(300, span_ms=10 * 60_000, first_id=1)
FRAMES = [ws_agg_frame(r) for r in TAPE.iter_rows(named=True)]


async def _no_sleep(_: float) -> None:
    return None


def _ids(batches: list[pl.DataFrame]) -> list[int]:
    return pl.concat(batches)["trade_id"].to_list() if batches else []


def test_gap_tracker() -> None:
    t = GapTracker("X")
    t.observe(TAPE.filter(pl.col("trade_id").is_in([1, 2, 3, 7, 8])))
    assert [(g.after_id, g.before_id, g.missing) for g in t.gaps] == [(3, 7, 3)]
    nxt = TAPE.filter(pl.col("trade_id").is_in([8, 9, 12]))
    fresh = t.drop_seen(nxt)
    assert fresh["trade_id"].to_list() == [9, 12] and t.duplicates_dropped == 1
    t.observe(fresh)
    assert t.gaps[-1].missing == 2


async def test_live_reconnect_backfills_gap() -> None:
    # Connection 1 delivers ids 1..100 then drops; connection 2 resumes at id 151
    # (with a duplicate of id 100). Ids 101..150 must come from REST.
    conn1: list[str | Exception] = [*FRAMES[:100], ConnectionError("dropped")]
    conn2: list[str | Exception] = [FRAMES[99], *FRAMES[150:]]
    adapter = FakeAdapter(TAPE, [conn1, conn2])
    src = LiveSource(
        adapter,
        ["BTCUSDT"],
        LiveConfig(backfill_page=20, watchdog_s=30),
        sleep=_no_sleep,
        max_connections=2,
    )
    got = [b.df async for b in src if b.kind == "trades"]
    assert _ids(got) == list(range(1, 301))
    assert adapter.rest_calls == [101, 121, 141]
    kinds = [e.kind for e in src.events]
    assert kinds.count("connect") == 2 and "gap" in kinds and "backfill" in kinds
    gaps = src.trackers["BTCUSDT"].gaps
    assert len(gaps) == 1 and gaps[0].resolved and gaps[0].missing == 50
    assert src.trackers["BTCUSDT"].duplicates_dropped == 1


async def test_live_backfill_failure_is_logged_as_unresolved() -> None:
    adapter = FakeAdapter(TAPE, [[*FRAMES[:10], *FRAMES[20:30]]], rest_fail=True)
    src = LiveSource(adapter, ["BTCUSDT"], LiveConfig(), sleep=_no_sleep, max_connections=1)
    got = [b.df async for b in src]
    assert _ids(got) == list(range(1, 11)) + list(range(21, 31))
    assert "backfill_failed" in [e.kind for e in src.events]
    gaps = src.trackers["BTCUSDT"].gaps
    assert [(g.after_id, g.before_id, g.resolved) for g in gaps] == [(10, 21, False)]


async def test_live_resume_from_stored_id_backfills() -> None:
    adapter = FakeAdapter(TAPE, [FRAMES[50:60]])
    src = LiveSource(
        adapter, ["BTCUSDT"], resume_ids={"BTCUSDT": 40}, sleep=_no_sleep, max_connections=1
    )
    got = [b.df async for b in src]
    assert _ids(got) == list(range(41, 61))


async def test_watchdog_triggers_reconnect() -> None:
    adapter = FakeAdapter(TAPE, [[*FRAMES[:3], "sleep:0.5"], FRAMES[3:6]])
    src = LiveSource(
        adapter, ["BTCUSDT"], LiveConfig(watchdog_s=0.1), sleep=_no_sleep, max_connections=2
    )
    got = [b.df async for b in src]
    assert _ids(got) == list(range(1, 7))
    assert "watchdog" in [e.kind for e in src.events]


async def test_max_lifetime_reconnects_without_backoff() -> None:
    # Each connection delivers its frames, then idles past the 0.2 s lifetime (real clock).
    adapter = FakeAdapter(TAPE, [[*FRAMES[:50], "sleep:1"], [*FRAMES[60:80], "sleep:1"]])
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    src = LiveSource(
        adapter,
        ["BTCUSDT"],
        LiveConfig(max_lifetime_s=0.2, watchdog_s=1e9),
        sleep=sleep,
        max_connections=2,
    )
    got = [b.df async for b in src]
    assert [e.kind for e in src.events].count("lifetime") == 2
    # Connection 2 starts at id 61, so ids 51..60 are backfilled from REST: the output is
    # contiguous.
    assert _ids(got) == list(range(1, 81))
    # Proactive reconnect does not back off (only backfill page pauses were slept).
    assert all(s == LiveConfig().backfill_pause_s for s in slept)


async def test_replay_and_live_produce_identical_minutes() -> None:
    """Same trades via ReplaySource (one batch) and LiveSource (one frame each)."""
    rep_sink, live_sink = MemorySink(), MemorySink()
    await Ingestor(CFG, ["BTCUSDT"], [rep_sink]).run(ReplaySource.from_frames("BTCUSDT", [TAPE]))
    adapter = FakeAdapter(TAPE, [FRAMES])
    src = LiveSource(adapter, ["BTCUSDT"], sleep=_no_sleep, max_connections=1)
    await Ingestor(CFG, ["BTCUSDT"], [live_sink]).run(src)
    assert rep_sink.ohlcv_df("BTCUSDT").equals(live_sink.ohlcv_df("BTCUSDT"))
    assert rep_sink.footprint_df("BTCUSDT").equals(live_sink.footprint_df("BTCUSDT"))


async def test_collector_writes_hourly_parts_and_resumes(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    adapter = FakeAdapter(TAPE, [FRAMES[:200]])
    rep = await run_collector(
        adapter,  # type: ignore[arg-type]
        CFG,
        store,
        ["BTCUSDT"],
        duration_s=3.0,  # wall clock; generous so a loaded CI box still drains 200 frames
        live_cfg=LiveConfig(watchdog_s=1e9),
    )
    assert rep["trades"]["BTCUSDT"] == 200
    day = dt.date(2026, 1, 1)
    raw = store.read("raw_aggtrades", "BTCUSDT", day, day)
    assert raw["trade_id"].to_list() == list(range(1, 201))
    oh = store.read("ohlcv_1m", "BTCUSDT", day, day)
    # The open (last) minute is not finalized; resume starts after the last finalized one.
    last_final = int(oh["last_id"].max())
    assert last_final < 200
    assert resume_ids(store, ["BTCUSDT"]) == {"BTCUSDT": last_final}
    assert (tmp_path / "logs").exists()


def test_hourly_sink_parts_are_session_unique(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    for session in ("s1", "s2"):
        sink = HourlyParquetSink(store, session=session)
        sink.on_trades("BTCUSDT", TAPE.head(10))
        sink.flush_all()
    files = list(store.files("raw_aggtrades", "BTCUSDT", dt.date(2026, 1, 1), dt.date(2026, 1, 1)))
    assert len(files) == 2


async def test_socket_is_drained_and_watchdog_quiet_during_slow_backfill() -> None:
    # Resume at id 40 -> the first frame (id 51) opens a gap. Each REST page takes 0.1 s
    # (two pages -> 0.2 s), longer than the 0.15 s watchdog. Frames keep arriving every
    # 10 ms. The reader must keep draining the socket during the backfill, and the
    # watchdog must not fire because trades are still being delivered.
    paced: list[str] = []
    for f in FRAMES[50:120]:
        paced += [f, "sleep:0.01"]
    adapter = FakeAdapter(TAPE, [paced], rest_delay_s=0.1)
    src = LiveSource(
        adapter,
        ["BTCUSDT"],
        LiveConfig(backfill_page=5, watchdog_s=0.15, backfill_pause_s=0.0),
        resume_ids={"BTCUSDT": 40},
        max_connections=1,
    )
    got = [b.df async for b in src]
    assert _ids(got) == list(range(41, 121))
    assert "watchdog" not in [e.kind for e in src.events]
    # While the first REST page was in flight, the reader kept pulling frames.
    assert adapter.yielded_at_rest[-1] > 5


async def test_backlog_is_drained_in_micro_batches_with_yields() -> None:
    # The scripted socket delivers 250 frames without awaiting, so they are all queued
    # before the consumer runs. They must be processed as one micro-batch (per-frame
    # processing costs ~9 ms, D-027), and the consumer must yield to the event loop between
    # batches: `await queue.get()` on a non-empty queue does not yield, and a backlog once
    # starved the reader task and the watchdog feed (seen live on 2026-11-26).
    import asyncio

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    t = asyncio.create_task(ticker())
    adapter = FakeAdapter(TAPE, [[*FRAMES[:250], "sleep:0.05", *FRAMES[250:]]])
    src = LiveSource(adapter, ["BTCUSDT"], LiveConfig(watchdog_s=30), max_connections=1)
    sizes = [b.df.height async for b in src]
    t.cancel()
    assert sum(sizes) == 300 and sizes[0] == 250
    assert ticks >= len(sizes)


async def test_gap_inside_a_micro_batch_is_backfilled() -> None:
    # Frames 1..20 and 31..60 arrive back to back and are merged into one batch; the gap
    # 21..30 is inside the batch, not at its start.
    adapter = FakeAdapter(TAPE, [[*FRAMES[:20], *FRAMES[30:60]]])
    src = LiveSource(adapter, ["BTCUSDT"], LiveConfig(watchdog_s=30), max_connections=1)
    got = [b.df async for b in src]
    assert _ids(got) == list(range(1, 61))
    gaps = src.trackers["BTCUSDT"].gaps
    assert [(g.after_id, g.before_id, g.resolved) for g in gaps] == [(20, 31, True)]
    assert adapter.rest_calls == [21]
