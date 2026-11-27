"""ITERATION-4 chunked aggTrade processing: identical output to the in-memory path."""

import datetime as dt
from pathlib import Path

import polars as pl
from polars.testing import assert_frame_equal

from flowlevels.cli import holdout_days_in
from flowlevels.data.bootstrap import run_days
from flowlevels.data.pipeline import find_time_holes, process_day
from flowlevels.data.storage import ParquetStore, day_start_ms
from tests.test_pipeline import CFG, DAY, VisionServer, _adapter, _day_trades


async def test_chunked_equals_in_memory(tmp_path: Path) -> None:
    server = VisionServer()
    trades = _day_trades(DAY, 7_000, 1, 11).filter(~pl.col("trade_id").is_in([300, 301]))
    # a silence long enough to be a time hole (repair disabled below: no raw trades served)
    s = day_start_ms(DAY)
    trades = trades.filter(~pl.col("ts_ms").is_between(s + 3_600_000, s + 3_600_000 + 600_000))
    server.add_day("BTCUSDT", DAY, trades)
    cfg = CFG.model_copy(
        update={
            "data_quality": CFG.data_quality.model_copy(update={"repair_holes_from_trades": False})
        }
    )
    a, b = ParquetStore(tmp_path / "a"), ParquetStore(tmp_path / "b")
    ra = await process_day(_adapter(server), a, cfg, "BTCUSDT", DAY, tmp_path / "ra")
    rb = await process_day(
        _adapter(server), b, cfg, "BTCUSDT", DAY, tmp_path / "rb", chunk_rows=997
    )
    for k in ("n_trades", "first_id", "last_id", "gaps", "holes", "status"):
        assert getattr(ra, k) == getattr(rb, k), k
    assert rb.holes and rb.holes == find_time_holes(
        trades, s, s + 86_400_000, cfg.data_quality.max_trade_silence_s
    )
    for ds in ("footprint_1m", "ohlcv_1m"):
        assert_frame_equal(a.read(ds, "BTCUSDT", DAY, DAY), b.read(ds, "BTCUSDT", DAY, DAY))
    assert list((tmp_path / "rb").glob("*.zip")) == []


async def test_run_days_by_month_and_disk_guard(tmp_path: Path) -> None:
    server = VisionServer()
    days = [dt.date(2025, 1, 31), dt.date(2025, 2, 1)]
    for i, d in enumerate(days):
        for j, sym in enumerate(("BTCUSDT", "ETHUSDT")):
            server.add_day(sym, d, _day_trades(d, 200, 1 + 1000 * i, 20 + i + 10 * j))
    store = ParquetStore(tmp_path)
    rep = await run_days(
        _adapter(server), store, CFG, ["BTCUSDT", "ETHUSDT"], days, tmp_path / "r",
        chunk_rows=50, by_month=True,
    )  # fmt: skip
    order = [(r["symbol"], r["day"]) for r in rep["days_report"]]
    assert order == [
        ("BTCUSDT", "2025-01-31"), ("ETHUSDT", "2025-01-31"),
        ("BTCUSDT", "2025-02-01"), ("ETHUSDT", "2025-02-01"),
    ]  # fmt: skip
    rep2 = await run_days(
        _adapter(server), ParquetStore(tmp_path / "x"), CFG, ["BTCUSDT"], days, tmp_path / "r",
        max_disk_pct=0.0,
    )  # fmt: skip
    assert rep2["days_report"] == [] and rep2["stopped"].startswith("disk")


def test_holdout_guard() -> None:
    p = CFG.plan
    inside = [p.holdout_start, p.holdout_end]
    outside = [p.a_end, p.b_start]
    assert holdout_days_in(CFG, inside + outside) == inside
