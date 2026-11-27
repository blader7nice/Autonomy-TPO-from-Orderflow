"""Paper engine: every minute, the research strategy code on live Binance 1m klines.

Same code path as research (DECISIONS PAPER-PLAN): `method5.prepare` + `method5.trades` on the
1m klines from a fixed anchor (paper-start UTC day - warm-up) to the last closed minute, costs
by `report.apply_costs`. Decided trades never change when more data arrives (no lookahead,
property-tested); if one did, it is recorded as a divergence and alerted, never overwritten.

The wall clock is used only for scheduling, lag/heartbeat and the weekly summary, never in
the trading rules (AGENTS.md rule 3).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import math
import os
import shutil
import time
import traceback
import zoneinfo
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from flowlevels.config import AppConfig, PaperVariant, config_hash, git_commit
from flowlevels.paper import charts, notify
from flowlevels.paper.feed import MIN, Fetch, KlineFeed
from flowlevels.paper.stats import iso_week, variant_stats
from flowlevels.paper.store import Store, now_ms
from flowlevels.paper.variants import grid_of, key_of, rules_hash
from flowlevels.research.report import apply_costs
from flowlevels.strategies import method5 as m5

log = logging.getLogger(__name__)
DAY = 86_400_000
LATE_MS = 3 * MIN
SENS = {
    "default": "net_r",
    "maker_entry": "net_r_maker",
    "slip_okx_2tick": "net_r_2tick",
    "slip_okx_4tick": "net_r_4tick",
}
ENTRY_KEYS = ("direction", "entry", "stop", "tp1", "tp2", "r_unit")


@dataclass
class Ledger:
    variant: PaperVariant
    version: str
    key: str
    paper_start: int
    run_start: int
    anchor: int


def _same(a: Any, b: Any) -> bool:
    fa = None if a is None or (isinstance(a, float) and math.isnan(a)) else a
    fb = None if b is None or (isinstance(b, float) and math.isnan(b)) else b
    if isinstance(fa, float) and isinstance(fb, float):
        return fa == fb
    return bool(fa == fb)


class Engine:
    def __init__(
        self,
        cfg: AppConfig,
        store: Store,
        fetch: Fetch,
        tg: notify.Telegram,
        data_dir: Path,
        dashboard_url: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.pc = cfg.paper
        self.store = store
        self.tg = tg
        self.sleep = sleep
        self.wall = wall
        self.data_dir = data_dir
        self.link = dashboard_url
        self.symbols = list(cfg.iteration5.symbols)
        self.feed = KlineFeed(
            data_dir / "klines",
            data_dir / "paper" / "klines",
            fetch,
            sleep,
            self.pc.backfill_page,
            self.pc.backfill_pace_s,
        )
        self.charts_dir = data_dir / "paper" / "charts"
        self.ledgers: list[Ledger] = []
        self.loops = 0
        self.started_ms = now_ms()
        self.last_flush = 0.0
        self.stalled: dict[str, int] = {}
        self.commit = git_commit()
        self.cfg_hash = config_hash(cfg)

    # ------------------------------------------------------------------ setup
    async def exchange_minute(self) -> int:
        """Open time of the forming 1m candle (exchange time) = first minute of paper trading."""
        df = await self.feed.fetch(self.symbols[0], None, 2)
        return int(df["minute_ts"][-1])

    def setup_ledgers(self, forming: int) -> list[str]:
        """One ledger per variant at its current rule version (new version = new ledger)."""
        notes = []
        warm = self.cfg.iteration5.warmup_days * DAY
        for v in self.pc.variants:
            ver = rules_hash(self.cfg, v)
            run_start = forming // DAY * DAY
            row = self.store.ensure_version(
                v.id,
                ver,
                key_of(v),
                v.model_dump(mode="json"),
                forming,
                run_start,
                run_start - warm,
            )
            if row["paper_start"] == forming:
                notes.append(f"{v.id} {key_of(v)}: new version {ver}")
            self.ledgers.append(
                Ledger(
                    v,
                    ver,
                    key_of(v),
                    int(row["paper_start"]),
                    int(row["run_start"]),
                    int(row["anchor"]),
                )
            )
        return notes

    async def start(self) -> None:
        prev = self.store.get_kv("heartbeat")
        forming = await self.exchange_minute()
        notes = self.setup_ledgers(forming)
        anchor = min(lg.anchor for lg in self.ledgers)
        for s in self.symbols:
            f = self.feed.load(s, anchor)
            n = await self.feed.backfill(s)
            miss = f.missing_minutes(anchor)
            log.info("%s: %d minutes loaded, %d backfilled, %d missing", s, f.df.height, n, miss)
            if miss:
                self.store.event(
                    "warning",
                    "missing_minutes",
                    f"{miss} missing 1m candles since the anchor (exchange data gap)",
                    s,
                )
        self.feed.flush()
        self.last_flush = self.wall()
        down = ""
        if prev is not None:
            gap_min = (now_ms() - prev[1]) / 60_000
            down = f" Previous heartbeat {gap_min:.0f} min ago."
        msg = (
            f"Paper engine started (commit {self.commit}, config {self.cfg_hash})."
            + down
            + (" " + "; ".join(notes) if notes else "")
        )
        self.store.event("info", "engine_start", msg)
        self.store.notify(
            "engine",
            "<b>Paper engine started</b>\n" + msg,
            dedupe=f"start:{self.started_ms}",
            enabled=self.tg.enabled,
        )
        self.store.set_kv("engine_state", "running")
        await asyncio.to_thread(self.compute_all, self.symbols)
        self.heartbeat()

    # ------------------------------------------------------------------ loop
    async def run_forever(self, max_loops: int | None = None) -> None:
        while max_loops is None or self.loops < max_loops:
            now = self.wall()
            nxt = (math.floor(now / 60) + 1) * 60 + self.pc.poll_offset_s
            await self.sleep(max(0.0, nxt - now))
            await self.step()

    async def step(self) -> None:
        self.loops += 1
        changed: set[str] = set()
        todo = list(self.symbols)
        for attempt in range(self.pc.poll_retries + 1):
            if attempt:  # a stale REST snapshot: another symbol already shows a newer minute
                await self.sleep(self.pc.poll_retry_s)
            for s in todo:
                try:
                    if await self.feed.poll(s, self.pc.poll_limit):
                        changed.add(s)
                except Exception as exc:  # network etc.: the stall check alerts if it persists
                    log.warning("poll %s failed: %s", s, type(exc).__name__)
                    msg = f"{type(exc).__name__}: {exc}"[:300]
                    self.store.event("warning", "poll_error", msg, s)
            todo = self.feed.lagging(self.symbols)
            if not todo:
                break
        if changed:
            await asyncio.to_thread(self.compute_all, [x for x in self.symbols if x in changed])
        if self.wall() - self.last_flush >= self.pc.flush_minutes * 60:
            await asyncio.to_thread(self.feed.flush)
            self.last_flush = self.wall()
        self.check_stalls()
        if self.loops % 10 == 1:
            self.check_disk()
        self.check_weekly()
        self.heartbeat()
        try:
            await notify.drain(self.store, self.tg)
        except Exception as exc:
            log.warning("notification drain failed: %s", type(exc).__name__)

    async def shutdown(self) -> None:
        self.feed.flush()
        self.store.set_kv("engine_state", "stopped")
        self.store.event("info", "engine_stop", "Paper engine stopped")
        self.store.notify(
            "engine",
            "<b>Paper engine stopped</b>",
            dedupe=f"stop:{self.started_ms}",
            enabled=self.tg.enabled,
        )
        with contextlib.suppress(Exception):
            await asyncio.wait_for(notify.drain(self.store, self.tg, 0.2), timeout=10)

    def heartbeat(self) -> None:
        last = {s: f.last_ts for s, f in self.feed.feeds.items()}
        self.store.set_kv(
            "heartbeat",
            {
                "ts": now_ms(),
                "loops": self.loops,
                "started": self.started_ms,
                "pid": os.getpid(),
                "commit": self.commit,
                "config": self.cfg_hash,
                "last_minute": last,
                "telegram": self.tg.enabled,
            },
        )

    # ------------------------------------------------------------------ compute
    def compute_all(self, symbols: list[str]) -> None:
        for s in symbols:
            t0 = time.perf_counter()
            try:
                bias = self.compute_symbol(s)
                ms = (time.perf_counter() - t0) * 1000
                f = self.feed.feeds[s]
                self.store.set_symbol_state(s, f.last_ts, float(f.df["close"][-1]), bias, ms)
            except Exception as exc:
                log.error("compute %s failed:\n%s", s, traceback.format_exc())
                self.store.event("error", "compute_error", f"{type(exc).__name__}: {exc}"[:500], s)
                hour = now_ms() // 3_600_000
                self.store.notify(
                    "health",
                    f"<b>Engine error</b> on {s}: {type(exc).__name__}",
                    dedupe=f"err:{s}:{hour}",
                    enabled=self.tg.enabled,
                )

    def compute_symbol(self, s: str) -> dict[str, Any] | None:
        f = self.feed.feeds[s]
        if f.last_ts is None:
            return None
        end = f.last_ts + MIN
        groups: dict[tuple[int, int], list[Ledger]] = {}
        for lg in self.ledgers:
            groups.setdefault((lg.anchor, lg.run_start), []).append(lg)
        bias = None
        for (anchor, run_start), lgs in groups.items():
            m = f.df.filter(pl.col("minute_ts") >= anchor)
            ctx = m5.prepare(m, run_start, end, self.cfg, s)
            sig: list[dict[str, Any]] = []
            rows = m5.trades(ctx, self.cfg, [grid_of(lg.variant) for lg in lgs], sig)
            if bias is None:
                payloads = charts.symbol_payloads(ctx, self.pc.chart_bars)
                self.store.set_charts(s, payloads)
                bias = payloads["overlay"]["bias"]
            costed = (
                apply_costs(
                    pl.DataFrame(rows, infer_schema_length=None), self.cfg, only=tuple(SENS)
                ).to_dicts()
                if rows
                else []
            )
            for lg in lgs:
                mine = [
                    r for r in costed if r["config"] == lg.key and r["entry_ts"] >= lg.paper_start
                ]
                self.reconcile(lg, s, ctx, mine, end)
                self.store.add_signals(
                    [
                        {**x, "variant": lg.variant.id, "version": lg.version, "detected_ts": end}
                        for x in sig
                        if x["config"] == lg.key and x["decision_ts"] >= lg.paper_start
                    ]
                )
        return bias

    def record(self, lg: Ledger, r: dict[str, Any], end: int) -> dict[str, Any]:
        is_open = bool(r["window_end_close"])
        tp1_hit = str(r["exit_kind"]).startswith("tp1")
        net = float(r["net_r_default"])
        t = {
            "variant": lg.variant.id,
            "version": lg.version,
            "symbol": r["symbol"],
            "config": r["config"],
            "trigger": r["trigger"],
            "direction": int(r["direction"]),
            "entry_ts": int(r["entry_ts"]),
            "entry": r["entry"],
            "stop": r["stop"],
            "r_unit": r["r_unit"],
            "far": r["far"],
            "stop_anchor": r.get("stop_anchor"),
            "atr1h": r["atr1h"],
            "tp1": r["tp1"],
            "tp2": r["tp2"],
            "tp1_r": r["tp1_r"],
            "ema4": r["ema4"],
            "emad": r["emad"],
            "close_d": r["close_d"],
            "pois": r["pois"],
            "a_ts": r["a_ts"],
            "b_ts": r["b_ts"],
            "trig_step": r["trig_step"],
            "extreme": r["extreme"],
            "pattern": r.get("pattern"),
            "swing": r["swing"],
            "status": "open" if is_open else "closed",
            "tp1_hit": int(tp1_hit),
            "x1_ts": r["x1_ts"] if (tp1_hit or not is_open) else None,
            "x1_px": r["x1_px"] if (tp1_hit or not is_open) else None,
            "x2_ts": None if is_open else r["x2_ts"],
            "x2_px": None if is_open else r["x2_px"],
            "exit_ts": None if is_open else r["exit_ts"],
            "exit_px": None if is_open else r["exit_px"],
            "exit_kind": None if is_open else r["exit_kind"],
            "gross_r": r["gross_r"],
            "net_r": net,
            "net_bps": r["bps_default"],
            "cost_r": float(r["gross_r"]) - net,
            "mfe_r": r["mfe_r"],
            "mae_r": r["mae_r"],
            "last_px": r["x2_px"] if is_open else r["exit_px"],
            "last_ts": end - MIN,
            "detected_ts": end,
            "detected_late": int(end - int(r["entry_ts"]) > LATE_MS),
            "updated_ts": now_ms(),
        }
        for sc, col in SENS.items():
            if sc != "default":
                t[col] = r[f"net_r_{sc}"]
        return t

    def reconcile(
        self, lg: Ledger, s: str, ctx: m5.Context, rows: list[dict[str, Any]], end: int
    ) -> None:
        existing = {
            int(t["entry_ts"]): t
            for t in self.store.rows(
                "SELECT * FROM trades WHERE variant=? AND version=? AND symbol=?",
                (lg.variant.id, lg.version, s),
            )
        }
        for r in rows:
            rec = self.record(lg, r, end)
            old = existing.pop(rec["entry_ts"], None)
            if old is not None:
                diff = [k for k in ENTRY_KEYS if not _same(old[k], rec[k])]
                if old["status"] == "closed":
                    diff += [k for k in ("exit_ts", "exit_px") if not _same(old[k], rec[k])]
                if diff:
                    self.divergence(lg, s, rec, f"fields changed: {', '.join(diff)}")
                    continue
                if old["status"] == "closed":
                    continue
            rec["chart_json"] = _json(charts.trade_payload(ctx, r))
            newly_closed = rec["status"] == "closed" and (old is None or old["status"] == "open")
            if old is None or newly_closed:
                rec["chart_png"] = self.render_png(lg, r, ctx, rec)
            elif old is not None:
                rec["chart_png"] = old["chart_png"]
            rec["id"] = self.store.upsert_trade(rec)
            en = self.tg.enabled
            dk = f"{lg.variant.id}:{lg.version}:{s}:{rec['entry_ts']}"
            if old is None:
                self.store.event(
                    "info",
                    "entry",
                    f"{lg.variant.id} {lg.key} {s} "
                    f"{'LONG' if rec['direction'] > 0 else 'SHORT'} @ {rec['entry']}",
                    s,
                )
                self.store.notify(
                    "entry",
                    notify.entry_text(rec, self.link),
                    rec["chart_png"],
                    dedupe=f"entry:{dk}",
                    enabled=en,
                )
            if rec["tp1_hit"] and (old is None or not old["tp1_hit"]) and rec["status"] == "open":
                self.store.notify("tp1", notify.tp1_text(rec), dedupe=f"tp1:{dk}", enabled=en)
            if newly_closed:
                self.store.event(
                    "info",
                    "exit",
                    f"{lg.variant.id} {lg.key} {s} {rec['exit_kind']} net {rec['net_r']:+.2f}R",
                    s,
                )
                self.store.notify(
                    "exit",
                    notify.exit_text(rec, self.link),
                    rec["chart_png"],
                    dedupe=f"exit:{dk}",
                    enabled=en,
                )
        for old in existing.values():
            self.divergence(lg, s, old, "stored trade no longer produced by the rules")

    def divergence(self, lg: Ledger, s: str, t: dict[str, Any], why: str) -> None:
        dk = f"div:{lg.variant.id}:{lg.version}:{s}:{t['entry_ts']}"
        if self.store.notify(
            "health",
            f"<b>DIVERGENCE</b> {lg.variant.id} {s} entry {t['entry_ts']}: {why}",
            dedupe=dk,
            enabled=self.tg.enabled,
        ):
            self.store.event(
                "error",
                "divergence",
                f"{lg.variant.id} {lg.key} entry {t['entry_ts']}: {why} (stored trade kept)",
                s,
            )

    def render_png(
        self, lg: Ledger, r: dict[str, Any], ctx: m5.Context, rec: dict[str, Any]
    ) -> str | None:
        from flowlevels.research.iter5 import chart_trade

        self.charts_dir.mkdir(parents=True, exist_ok=True)
        p = self.charts_dir / f"{lg.variant.id}-{lg.version}-{r['symbol']}-{r['entry_ts']}.png"
        side = "LONG" if r["direction"] > 0 else "SHORT"
        state = (
            f"open, {rec['net_r']:+.2f} R if closed now"
            if rec["status"] == "open"
            else f"{rec['exit_kind']}, net {rec['net_r']:+.2f} R"
        )
        title = (
            f"PAPER {lg.variant.id} {lg.key}: {r['symbol']} {side} "
            f"{dt.datetime.fromtimestamp(r['entry_ts'] / 1000, dt.UTC):%Y-%m-%d %H:%M} UTC, "
            f"{state}"
        )
        try:
            chart_trade(r, ctx.m, self.cfg, p, title)
        except Exception as exc:
            log.warning("chart render failed: %s", exc)
            return None
        return str(p)

    # ------------------------------------------------------------------ health
    def check_stalls(self) -> None:
        now = now_ms()
        for s, f in self.feed.feeds.items():
            last = f.last_ts
            if last is None:
                continue
            lag = now - (last + MIN)
            if lag > self.pc.stall_alert_minutes * MIN:
                if s not in self.stalled:
                    self.stalled[s] = last
                    self.store.event(
                        "warning", "stall", f"no new closed minute for {lag / MIN:.0f} min", s
                    )
                    self.store.notify(
                        "health",
                        f"<b>Data stall</b> {s}: no new 1m candle for {lag / MIN:.0f} min",
                        dedupe=f"stall:{s}:{last}",
                        enabled=self.tg.enabled,
                    )
            elif s in self.stalled:
                since = self.stalled.pop(s)
                self.store.event("info", "stall_recovered", "data flowing again", s)
                self.store.notify(
                    "health",
                    f"<b>Data recovered</b> {s}",
                    dedupe=f"rec:{s}:{since}",
                    enabled=self.tg.enabled,
                )

    def check_disk(self) -> None:
        u = shutil.disk_usage(self.data_dir)
        pct = 100 * u.used / u.total
        self.store.set_kv("disk", {"pct": pct, "free_gb": u.free / 1e9, "total_gb": u.total / 1e9})
        if pct > self.pc.disk_alert_pct:
            day = dt.datetime.now(dt.UTC).date().isoformat()
            self.store.notify(
                "health",
                f"<b>Disk usage {pct:.0f}%</b> (limit {self.pc.disk_alert_pct:.0f}%)",
                dedupe=f"disk:{day}",
                enabled=self.tg.enabled,
            )

    def check_weekly(self) -> None:
        tz = zoneinfo.ZoneInfo(self.pc.summary_timezone)
        local = dt.datetime.fromtimestamp(self.wall(), tz)
        hh, mm = (int(x) for x in self.pc.summary_time.split(":"))
        if local.weekday() != self.pc.summary_weekday or (local.hour, local.minute) < (hh, mm):
            return
        week = iso_week(int(self.wall() * 1000))
        if self.store.get_kv("weekly_sent") and self.store.get_kv("weekly_sent")[0] == week:  # type: ignore[index]
            return
        rows = ledger_stats(self.store, self.cfg)
        self.store.notify(
            "weekly",
            notify.weekly_text(week, rows, self.link),
            dedupe=f"weekly:{week}",
            enabled=self.tg.enabled,
        )
        self.store.set_kv("weekly_sent", week)


def ledger_stats(store: Store, cfg: AppConfig) -> list[dict[str, Any]]:
    """Per active ledger: stats of its trades (used by the weekly summary and the dashboard)."""
    pc = cfg.paper
    out = []
    for v in store.versions():
        trades = store.rows(
            "SELECT * FROM trades WHERE variant=? AND version=? ORDER BY entry_ts",
            (v["variant"], v["version"]),
        )
        st = variant_stats(
            trades, pc.ci_min_trades, pc.freeze_trades, pc.decision_trades, pc.ci_lower_min_r
        )
        bt = pc.backtest.get(v["variant"])
        out.append(
            {
                "variant": v["variant"],
                "version": v["version"],
                "key": v["key"],
                "paper_start": v["paper_start"],
                "stats": st,
                "backtest": bt.model_dump() if bt else None,
            }
        )
    return out


def _json(x: Any) -> str:
    return json.dumps(clean(x), separators=(",", ":"), allow_nan=False)


def clean(x: Any) -> Any:
    """NaN/inf -> None, recursively (JSON for the browser)."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [clean(v) for v in x]
    return x
