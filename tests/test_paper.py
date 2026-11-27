"""Paper engine: same trades as the research code path, restart safety, gap backfill,
notifications (deduplicated, token never stored), stats and the Telegram client."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import polars as pl

from flowlevels.config import load_config
from flowlevels.paper import notify
from flowlevels.paper.engine import Engine
from flowlevels.paper.feed import KlineFeed, complete_rows
from flowlevels.paper.stats import session_ci, variant_stats
from flowlevels.paper.store import Store
from flowlevels.paper.variants import rules_hash, variant_grids
from flowlevels.strategies import method5 as m5

ROOT = Path(__file__).resolve().parents[1]
CFG0 = load_config(ROOT / "configs/default.yaml")
DAY, MIN = 86_400_000, 60_000
T0 = 1_704_067_200_000  # 2024-01-01


def _cfg():  # type: ignore[no-untyped-def]
    i5 = CFG0.iteration5.model_copy(
        update={
            "tp1_min_r": 0.3,
            "sweep_range_k_atr15": 0.5,
            "warmup_days": 60,
            "symbols": ["BTCUSDT"],
        }
    )
    pc = CFG0.paper.model_copy(update={"stall_alert_minutes": 10**9, "backfill_pace_s": 0.2})
    return CFG0.model_copy(update={"iteration5": i5, "paper": pc})


def _klines(days: int, seed: int) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    n = days * 1440
    drift = np.repeat(rng.normal(0, 6, days), 1440)
    c = 90_000 + np.cumsum(rng.standard_t(4, n) * 15.0 + drift / 60)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.exponential(8, n)
    lo = np.minimum(o, c) - rng.exponential(8, n)
    return pl.DataFrame({"minute_ts": T0 + np.arange(n) * MIN, "open": o, "high": h, "low": lo,
                         "close": c, "volume": rng.exponential(10, n)})  # fmt: skip


class FakeExchange:
    """REST klines up to `now` (the forming minute, returned last like Binance)."""

    def __init__(self, df: pl.DataFrame, now: int) -> None:
        self.df, self.now, self.calls = df, now, 0

    async def fetch(self, symbol: str, start: int | None, limit: int) -> pl.DataFrame:
        self.calls += 1
        x = self.df.filter(pl.col("minute_ts") <= self.now)
        if start is None:
            return x.tail(limit)
        return x.filter(pl.col("minute_ts") >= start).head(limit)


async def _nosleep(_: float) -> None:
    return None


def _engine(tmp: Path, ex: FakeExchange, cfg: Any) -> Engine:
    store = Store(tmp / "paper" / "paper.sqlite")
    wall = dt.datetime(2026, 11, 25, 12, tzinfo=dt.UTC).timestamp  # a Wednesday
    return Engine(cfg, store, ex.fetch, notify.Telegram(None, None), tmp, "https://x.ts.net",
                  sleep=_nosleep, wall=wall)  # fmt: skip


def _setup(tmp: Path, seed: int = 11, hist_days: int = 70, total_days: int = 84):  # type: ignore[no-untyped-def]
    full = _klines(total_days, seed)
    (tmp / "klines" / "BTCUSDT").mkdir(parents=True)
    full.filter(pl.col("minute_ts") < T0 + hist_days * DAY).write_parquet(
        tmp / "klines" / "BTCUSDT" / "hist.parquet"
    )
    return full, FakeExchange(full, T0 + hist_days * DAY + 37 * MIN)


def test_engine_matches_research_and_survives_restart(tmp_path: Path) -> None:
    cfg = _cfg()
    full, ex = _setup(tmp_path)
    eng = _engine(tmp_path, ex, cfg)
    asyncio.run(eng.start())
    lg = eng.ledgers[0]
    assert lg.paper_start == T0 + 70 * DAY + 37 * MIN and lg.run_start == T0 + 70 * DAY
    assert lg.anchor == lg.run_start - 60 * DAY
    for i in range(20):  # 5 days in 6-hour jumps (each one a gap -> backfill)
        ex.now += 6 * 60 * MIN
        asyncio.run(eng.step())
        if i == 9:  # restart half-way: a new engine on the same DB and stores
            eng.feed.flush()
            eng.store.close()
            eng = _engine(tmp_path, ex, cfg)
            asyncio.run(eng.start())
            assert [x.paper_start for x in eng.ledgers] == [lg.paper_start] * 4
    end = ex.now  # forming minute = exclusive end of closed data
    m = full.filter((pl.col("minute_ts") >= lg.anchor) & (pl.col("minute_ts") < end))
    exp, _ = m5.run_symbol(m, lg.run_start, end, cfg, "BTCUSDT", variant_grids(cfg))
    exp = [r for r in exp if r["entry_ts"] >= lg.paper_start]
    got = eng.store.rows("SELECT * FROM trades ORDER BY config, entry_ts")
    assert exp, "synthetic data should give paper trades"
    key = lambda r: (r["config"], r["entry_ts"])  # noqa: E731
    assert sorted(map(key, exp)) == [key(r) for r in got]
    by = {key(r): r for r in exp}
    for t in got:
        r = by[key(t)]
        for f in ("direction", "entry", "stop", "tp1", "r_unit"):
            assert t[f] == r[f]
        assert (t["status"] == "open") == r["window_end_close"]
        if t["status"] == "closed":
            assert (t["exit_ts"], t["exit_px"], t["exit_kind"]) == (
                r["exit_ts"],
                r["exit_px"],
                r["exit_kind"],
            )
        assert t["detected_late"] == 1  # 6-hour jumps
        assert json.loads(t["chart_json"])["bars"]
    # one entry and at most one exit notification per trade, despite the restart
    nots = eng.store.rows("SELECT kind, dedupe, status FROM notifications")
    n_entry = sum(1 for n in nots if n["kind"] == "entry")
    n_exit = sum(1 for n in nots if n["kind"] == "exit")
    assert n_entry == len(got)
    assert n_exit == sum(1 for t in got if t["status"] == "closed")
    assert all(n["status"] == "disabled" for n in nots)
    assert not eng.store.rows("SELECT * FROM events WHERE kind='divergence'")
    assert eng.store.rows("SELECT count(*) AS n FROM signals")[0]["n"] >= len(got)
    # the live store holds every fetched minute exactly once (after the periodic flush)
    eng.feed.flush()
    live = pl.read_parquet(sorted((tmp_path / "paper" / "klines" / "BTCUSDT").glob("*.parquet")))
    assert live["minute_ts"].is_unique().all() and int(live["minute_ts"].max()) == end - MIN


def test_divergence_is_flagged_not_overwritten(tmp_path: Path) -> None:
    cfg = _cfg()
    _, ex = _setup(tmp_path, seed=11)
    eng = _engine(tmp_path, ex, cfg)
    asyncio.run(eng.start())
    for _ in range(12):
        ex.now += 6 * 60 * MIN
        asyncio.run(eng.step())
    t = eng.store.rows("SELECT * FROM trades ORDER BY entry_ts LIMIT 1")[0]
    with eng.store.tx() as db:
        db.execute("UPDATE trades SET stop=stop+1 WHERE id=?", (t["id"],))
    ex.now += MIN
    asyncio.run(eng.step())
    ev = eng.store.rows("SELECT * FROM events WHERE kind='divergence'")
    assert len(ev) == 1
    assert eng.store.one("SELECT stop FROM trades WHERE id=?", (t["id"],))["stop"] == t["stop"] + 1  # type: ignore[index]


def test_versions_change_with_rules_only() -> None:
    v = CFG0.paper.variants[0]
    h = rules_hash(CFG0, v)
    ops = CFG0.paper.model_copy(update={"chart_bars": 123, "poll_limit": 9})
    assert rules_hash(CFG0.model_copy(update={"paper": ops}), v) == h
    i5 = CFG0.iteration5.model_copy(update={"trail_k_atr": 2.0})
    assert rules_hash(CFG0.model_copy(update={"iteration5": i5}), v) != h
    assert rules_hash(CFG0, v.model_copy(update={"k": 0.25})) != h
    assert len({rules_hash(CFG0, x) for x in CFG0.paper.variants}) == 4


def test_new_version_gets_new_ledger(tmp_path: Path) -> None:
    store = Store(tmp_path / "p.sqlite")
    a = store.ensure_version("a", "v1", "k", {}, 100, 0, -1)
    b = store.ensure_version("a", "v1", "k", {}, 999, 0, -1)
    assert a["paper_start"] == b["paper_start"] == 100
    c = store.ensure_version("a", "v2", "k", {}, 500, 0, -1)
    assert c["paper_start"] == 500
    assert [v["version"] for v in store.versions()] == ["v2"]


def test_feed_drops_forming_candle_and_backfills_gap(tmp_path: Path) -> None:
    full = _klines(3, 1)
    ex = FakeExchange(full, T0 + 100 * MIN)
    feed = KlineFeed(tmp_path / "none", tmp_path / "live", ex.fetch, _nosleep, page=30)
    f = feed.load("BTCUSDT", T0)
    asyncio.run(feed.backfill("BTCUSDT"))
    assert f.last_ts == T0 + 99 * MIN and f.df.height == 100  # forming minute 100 excluded
    ex.now += 3 * MIN
    assert asyncio.run(feed.poll("BTCUSDT", 5)) == 3 and f.last_ts == T0 + 102 * MIN
    ex.now += 200 * MIN  # gap larger than a poll -> backfill
    assert asyncio.run(feed.poll("BTCUSDT", 5)) == 200
    assert f.df["minute_ts"].diff().drop_nulls().unique().to_list() == [MIN]
    assert complete_rows(full.head(1)).height == 0


def test_stats_ci_and_rule() -> None:
    rng = np.random.default_rng(0)
    tr = [{"symbol": "BTCUSDT" if i % 2 else "ETHUSDT", "entry_ts": T0 + i * DAY // 3,
           "exit_ts": T0 + i * DAY // 3 + 1, "net_r": float(x), "net_bps": float(x) * 50,
           "status": "closed"} for i, x in enumerate(rng.normal(0.3, 1.0, 60))]  # fmt: skip
    ci = session_ci(tr)
    assert ci is not None and ci[1] < ci[0] < ci[2]
    st = variant_stats(tr, 10, 30, 50, -0.1)
    assert st["n_closed"] == 60 and st["progress"]["decision_rule_met"] == (
        ci[0] > 0 and ci[1] > -0.1
    )
    assert abs(st["equity"][-1][1] - sum(t["net_r"] for t in tr)) < 1e-9
    few = variant_stats(tr[:5], 10, 30, 50, -0.1)
    assert few["ci"] is None and few["progress"]["frozen"]


def test_telegram_never_leaks_token() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path.endswith("/sendMessage"):
            return httpx.Response(
                400, json={"ok": False, "description": "Bad Request: chat not found"}
            )
        return httpx.Response(200, json={"ok": True})

    tg = notify.Telegram(
        "123:SECRET", "42", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    ok, err = asyncio.run(tg.send("hello"))
    assert not ok and err is not None and "SECRET" not in err and "chat not found" in err
    assert "bot123:SECRET/sendMessage" in str(seen[0].url)
    assert not notify.Telegram(None, "42").enabled


def test_notification_outbox_order_and_retry(tmp_path: Path) -> None:
    store = Store(tmp_path / "p.sqlite")
    assert store.notify("x", "one", dedupe="k1")
    assert not store.notify("x", "one again", dedupe="k1")
    store.notify("x", "two", dedupe="k2")
    sent: list[str] = []
    fail = {"n": 1}

    class TG:
        enabled = True

        async def send(self, text: str, photo: str | None = None) -> tuple[bool, str | None]:
            if fail["n"]:
                fail["n"] -= 1
                return False, "HTTP 502"
            sent.append(text)
            return True, None

    asyncio.run(notify.drain(store, TG(), 0))  # type: ignore[arg-type]
    assert sent == []  # first failed: keep order, retry next loop
    asyncio.run(notify.drain(store, TG(), 0))  # type: ignore[arg-type]
    assert sent == ["one", "two"]
