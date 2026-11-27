"""DEMO database for dashboard screenshots (DECISIONS PAPER-PLAN): window B replayed through
the paper engine itself (same code), written to a separate SQLite file and always shown with a
DEMO banner. Never read by the live engine. B is in-sample data; nothing from the hold-out is
traded (B's warm-up only, as in research)."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from flowlevels.config import AppConfig
from flowlevels.paper import notify
from flowlevels.paper.engine import Engine
from flowlevels.paper.feed import MIN
from flowlevels.paper.store import Store, now_ms
from flowlevels.research.plan import windows

DAY = 86_400_000


def pick_cut(ref_trades: Path, start: int, end: int, min_open: int = 3) -> int:
    """A minute in B (on a 30m boundary) with at least `min_open` open reference trades and
    as late as possible, so the demo shows open positions and a long closed history."""
    t = pl.read_parquet(ref_trades).filter(pl.col("window") == "B")
    best = end - MIN
    for c in range(end - 2 * DAY, start + 20 * DAY, -1_800_000):
        cut = c + 7 * MIN
        n = t.filter((pl.col("entry_ts") < cut) & (pl.col("exit_ts") > cut + 60 * MIN)).height
        if n >= min_open:
            return cut
    return best


def build(cfg: AppConfig, data_dir: Path, out_dir: Path, ref_trades: Path) -> Path:
    start, end = windows(cfg)["B"]
    cut = pick_cut(ref_trades, start, end)
    out_dir.mkdir(parents=True, exist_ok=True)
    link = out_dir / "klines"
    if not link.exists():
        os.symlink((data_dir / "klines").resolve(), link)
    db = out_dir / "paper" / "paper.sqlite"
    for p in (db, db.with_suffix(".sqlite-wal"), db.with_suffix(".sqlite-shm")):
        p.unlink(missing_ok=True)
    store = Store(db)

    async def nofetch(symbol: str, s: int | None, limit: int) -> pl.DataFrame:
        raise RuntimeError("demo build is offline")

    eng = Engine(cfg, store, nofetch, notify.Telegram(None, None), out_dir)
    eng.setup_ledgers(start)
    anchor = min(lg.anchor for lg in eng.ledgers)
    for s in eng.symbols:
        f = eng.feed.load(s, anchor)
        f.df = f.df.filter(pl.col("minute_ts") < cut)
    eng.compute_all(eng.symbols)
    with store.tx() as c:
        c.execute("UPDATE trades SET detected_late=0, detected_ts=entry_ts+60000")
        c.execute("DELETE FROM notifications")
        c.execute("DELETE FROM events")
    store.set_kv("demo_now", str(cut + 25_000))
    store.set_kv("heartbeat", {
        "ts": now_ms(), "loops": 1440, "started": now_ms() - 6 * 3_600_000, "pid": 0,
        "commit": eng.commit, "config": eng.cfg_hash, "telegram": False, "last_minute": {},
    })  # fmt: skip
    store.set_kv("engine_state", "running")
    for s in eng.symbols:
        when = datetime.fromtimestamp(cut / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        store.event("info", "demo", f"demo ledger: window B replayed up to {when}", s)
    asyncio.run(asyncio.sleep(0))
    store.close()
    return db
