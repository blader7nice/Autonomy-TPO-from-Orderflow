"""Paper-test dashboard: read-only FastAPI backend + static frontend (DECISIONS PAPER-PLAN).

Binds to 127.0.0.1 only (remote access through `tailscale serve`). Opens the engine's SQLite
read-only and has no write endpoints. Live updates: a WebSocket that tells the browser to
refetch when the database changes. If the engine heartbeat goes stale, this process sends a
Telegram alert itself (the engine cannot when it is down).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from flowlevels.config import AppConfig
from flowlevels.paper import notify, verify
from flowlevels.paper.engine import clean, ledger_stats
from flowlevels.paper.store import Store, now_ms
from flowlevels.web import readout

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
MIN = 60_000
TF_MS = {"15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}
VARIANT_COLORS = {"a": "#9887ef", "b": "#22d3ee", "c": "#ec4899", "d": "#f1f5f9"}


class Db:
    """Lazily opened read-only store (the engine may start after the web process)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._s: Store | None = None

    def get(self) -> Store | None:
        if self._s is None and self.path.exists():
            try:
                self._s = Store(self.path, readonly=True)
                self._s.data_version()
            except Exception:
                self._s = None
        return self._s


def _j(x: Any) -> JSONResponse:
    return JSONResponse(clean(x))


def _clock(s: Store, demo: bool) -> int:
    """'Now' for lag displays: the wall clock, or the replay time of a demo database."""
    if demo:
        d = s.get_kv("demo_now")
        if d:
            return int(d[0])
    return now_ms()


def _engine_state(s: Store, cfg: AppConfig, demo: bool = False) -> dict[str, Any]:
    hb = s.get_kv("heartbeat")
    st = s.get_kv("engine_state")
    if hb is None:
        return {"status": "unknown"}
    info = json.loads(hb[0])
    if demo:
        return {**info, "status": "demo", "heartbeat_age_s": 2.0}
    age = (now_ms() - int(info["ts"])) / 1000
    status = "running"
    if st and st[0] == "stopped":
        status = "stopped"
    elif age > cfg.paper.heartbeat_stale_minutes * 60:
        status = "stale"
    return {"status": status, "heartbeat_age_s": age, **info}


def _reasons(t: dict[str, Any]) -> list[dict[str, Any]]:
    d = int(t["direction"])
    side = "long" if d > 0 else "short"
    cmp = ">" if d > 0 else "<"
    k = "0.1" if t.get("stop_anchor") is not None else "0.5"
    anchor = (
        f"confirmation-candle wick {t['stop_anchor']:.6g}"
        if t.get("stop_anchor") is not None
        else f"POI far edge {t['far']:.6g}"
    )
    raw = (t["stop_anchor"] if t.get("stop_anchor") is not None else t["far"]) - d * float(
        k
    ) * float(t["atr1h"])
    widened = abs(raw - float(t["stop"])) > 1e-12
    trig = (
        f"15m sweep of the POI and the last 15m swing at {_utc(t['a_ts'])}, close back at "
        f"{_utc(t['b_ts'])} with a confirming candle"
        if t["trigger"] == "T1"
        else f"30m bar touched the POI at {_utc(t['a_ts'])}; confirmation candle "
        f"({t.get('pattern') or 'engulf/pin'}) closed at {_utc(int(t['b_ts']) + 1_800_000)}, "
        "not beyond the far edge"
    )
    second = "next structural level (TP2)" if t["config"].endswith("lvl") else "1.5 × ATR(1h) trail"
    return [
        {
            "rule": "Bias (4H)",
            "ok": True,
            "text": f"4H close {cmp} EMA50 and EMA50 rising"
            if d > 0
            else "4H close < EMA50 and EMA50 falling",
        },
        {
            "rule": "Entry vs 4H EMA50",
            "ok": True,
            "text": f"entry {t['entry']:.6g} {cmp} 4H EMA50 {t['ema4']:.6g}",
        },
        {
            "rule": "Daily bias",
            "ok": True,
            "text": f"daily close {t['close_d']:.6g} {cmp} daily EMA50 {t['emad']:.6g}",
        },
        {"rule": "POI (first test)", "ok": True, "text": str(t["pois"]).replace(";", " · ")},
        {"rule": "Trigger", "ok": True, "text": trig},
        {
            "rule": "Stop",
            "ok": True,
            "text": f"{anchor} {'−' if d > 0 else '+'} {k} × ATR(1h) {t['atr1h']:.4g} = {raw:.6g}"
            + (
                f"; widened to the minimum distance → {t['stop']:.6g}"
                if widened
                else f" → {t['stop']:.6g}"
            )
            + f" (1R = {float(t['r_unit']) / float(t['entry']) * 1e4:.0f} bps)",
        },
        {
            "rule": "TP1 ≥ 1R",
            "ok": True,
            "text": (
                f"nearest structural level {t['tp1']:.6g} = {t['tp1_r']:.2f}R; 50 % exits there"
            ),
        },
        {"rule": "Second half", "ok": True, "text": second},
        {
            "rule": "Limits",
            "ok": True,
            "text": f"first {side} trade of the UTC day, no open position",
        },
    ]


def _utc(ms: int | None) -> str:
    if ms is None:
        return "–"
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.UTC).strftime("%m-%d %H:%M")


def create_app(cfg: AppConfig, db_path: Path, data_dir: Path, demo: bool = False,
               tg: notify.Telegram | None = None, rest_url: str | None = None,
               verify_loop: bool = True) -> FastAPI:  # fmt: skip
    db = Db(db_path)
    symbols = list(cfg.iteration5.symbols)
    fetch = verify.rest_fetcher(rest_url) if rest_url and not demo else None
    candles = verify.Candles(data_dir, fetch)
    vstate: dict[str, Any] = {"result": None, "running": False, "last_run": 0.0}
    vlock = asyncio.Lock()

    async def run_verify() -> dict[str, Any] | None:
        s = db.get()
        if s is None:
            return None
        async with vlock:
            vstate["running"] = True
            try:
                res = await asyncio.to_thread(
                    verify.run, cfg, s, candles, symbols, fetch is not None
                )
            finally:
                vstate["running"] = False
                vstate["last_run"] = asyncio.get_running_loop().time()
        prev = vstate["result"]
        vstate["result"] = res
        if tg is not None and tg.enabled and not demo:
            await _verify_alert(tg, prev, res)
        return res

    async def verify_loop_task() -> None:
        await asyncio.sleep(45)
        while True:
            try:
                await run_verify()
            except Exception as exc:  # never let the check kill the web process
                log.warning("verify error: %s", type(exc).__name__)
            await asyncio.sleep(cfg.paper.verify_minutes * 60)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        tasks = []
        if tg is not None and tg.enabled and not demo:
            tasks.append(asyncio.create_task(_watchdog(db, cfg, tg)))
        if verify_loop:
            tasks.append(asyncio.create_task(verify_loop_task()))
        yield
        for task in tasks:
            task.cancel()

    app = FastAPI(title="flowlevels paper", docs_url=None, redoc_url=None, lifespan=lifespan)

    def store() -> Store:
        s = db.get()
        if s is None:
            raise HTTPException(503, "paper database not available yet (engine not started)")
        return s

    def variants(s: Store) -> list[dict[str, Any]]:
        return [
            {
                "id": v["variant"],
                "key": v["key"],
                "version": v["version"],
                "paper_start": v["paper_start"],
                "color": VARIANT_COLORS.get(v["variant"], "#cbd5e1"),
            }
            for v in s.versions()
        ]

    @app.get("/api/summary")
    def summary() -> JSONResponse:
        s = db.get()
        base: dict[str, Any] = {"demo": demo, "symbols": symbols, "server_ms": now_ms()}
        if s is None:
            return _j({**base, "engine": {"status": "unknown"}, "variants": [], "open": 0})
        states = s.rows("SELECT symbol, last_minute_ts, updated_ts FROM symbol_state")
        clk = _clock(s, demo)
        lags = [clk - (int(x["last_minute_ts"]) + MIN) for x in states if x["last_minute_ts"]]
        n_open = len(s.open_trades())
        last_upd = max((int(x["updated_ts"]) for x in states), default=None)
        if demo and last_upd is not None:
            last_upd = now_ms() - 3000
        return _j({
            **base,
            "engine": _engine_state(s, cfg, demo),
            "variants": variants(s),
            "open": n_open,
            "max_lag_s": max(lags) / 1000 if lags else None,
            "last_update_ms": last_upd,
            "thresholds": {
                "freeze": cfg.paper.freeze_trades,
                "decision": cfg.paper.decision_trades,
                "ci_lower_min_r": cfg.paper.ci_lower_min_r,
                "ci_min": cfg.paper.ci_min_trades,
            },
        })  # fmt: skip

    @app.get("/api/chart/{symbol}/{tf}")
    def chart(symbol: str, tf: str) -> JSONResponse:
        if symbol not in symbols or tf not in TF_MS:
            raise HTTPException(404, "unknown symbol or timeframe")
        s = store()
        r = s.one(
            "SELECT payload, updated_ts FROM chart_cache WHERE symbol=? AND tf=?", (symbol, tf)
        )
        ov = s.one("SELECT payload FROM chart_cache WHERE symbol=? AND tf='overlay'", (symbol,))
        if r is None or ov is None:
            return _j({"empty": True})
        p = json.loads(r["payload"])
        step = TF_MS[tf]
        t_from = int(p["bars"][0][0]) * 1000 if p["bars"] else 0
        vmap = {v["id"]: v for v in variants(s)}
        trades = [
            t
            for t in s.rows(
                "SELECT t.* FROM trades t JOIN versions v ON v.variant=t.variant AND "
                "v.version=t.version AND v.active=1 WHERE t.symbol=? AND "
                "(t.exit_ts IS NULL OR t.exit_ts >= ?) ORDER BY t.entry_ts",
                (symbol, t_from),
            )
        ]
        markers = []
        for t in trades:
            d = int(t["direction"])
            col = vmap.get(t["variant"], {}).get("color", "#cbd5e1")
            if int(t["entry_ts"]) >= t_from:
                markers.append({
                    "time": int(t["entry_ts"]) // step * step // 1000,
                    "position": "belowBar" if d > 0 else "aboveBar",
                    "shape": "arrowUp" if d > 0 else "arrowDown",
                    "kind": "entry", "dir": d, "variant": t["variant"], "color": col,
                    "text": f"{t['variant']} {'L' if d > 0 else 'S'}",
                })  # fmt: skip
            for xt, lab in ((t["x1_ts"], "x1"), (t["x2_ts"], "x2")):
                if (
                    xt is not None
                    and int(xt) >= t_from
                    and not (lab == "x2" and t["x2_ts"] == t["x1_ts"])
                ):
                    markers.append({
                        "time": int(xt) // step * step // 1000,
                        "position": "aboveBar" if d > 0 else "belowBar",
                        "shape": "circle", "kind": "exit", "variant": t["variant"],
                        "win": (t["net_r"] or 0) > 0 if t["status"] == "closed" else (lab == "x1"),
                        "text": t["variant"],
                    })  # fmt: skip
        sig = s.rows(
            "SELECT * FROM signals WHERE symbol=? AND decision_ts >= ? AND outcome != 'taken' "
            "ORDER BY decision_ts",
            (symbol, t_from),
        )
        seen: set[tuple[int, int]] = set()
        for g in sig:
            k = (int(g["decision_ts"]), int(g["direction"]))
            if k in seen:
                continue
            seen.add(k)
            markers.append({
                "time": (int(g["decision_ts"]) - 1) // step * step // 1000,
                "position": "belowBar" if g["direction"] > 0 else "aboveBar",
                "shape": "circle", "kind": "signal", "dir": int(g["direction"]),
                "text": "", "outcome": g["outcome"],
            })  # fmt: skip
        markers.sort(key=lambda m: m["time"])
        open_pos = [
            {
                "id": t["id"], "variant": t["variant"], "dir": int(t["direction"]),
                "entry": t["entry"], "stop": t["stop"], "tp1": t["tp1"], "tp2": t["tp2"],
                "tp1_hit": bool(t["tp1_hit"]), "exit2": t["config"].split("|")[-1],
                "color": vmap.get(t["variant"], {}).get("color"), "net_r": t["net_r"],
            }
            for t in trades
            if t["status"] == "open"
        ]  # fmt: skip
        state = s.one("SELECT * FROM symbol_state WHERE symbol=?", (symbol,))
        overlay = json.loads(ov["payload"])
        rd: dict[str, Any] = {}
        anchor = verify.anchor_of(s)
        if anchor is not None:
            try:
                df, _ = candles.get(symbol, anchor)
                rd = readout.for_chart(cfg, symbol, step, p["bars"], int(overlay["end"]) * 1000, df)
            except Exception as exc:
                log.warning("readout %s: %s", symbol, exc)
                rd = {"error": type(exc).__name__}
        return _j({
            **p, "overlay": overlay, "markers": markers,
            "positions": open_pos, "updated_ts": r["updated_ts"],
            "last_minute_ts": state["last_minute_ts"] if state else None, "readout": rd,
        })  # fmt: skip

    @app.get("/api/positions")
    def positions() -> JSONResponse:
        s = store()
        vmap = {v["id"]: v for v in variants(s)}
        out = []
        for t in s.open_trades():
            out.append({**_trade_row(t), "color": vmap.get(t["variant"], {}).get("color")})
        return _j({"positions": out, "variants": list(vmap.values())})

    @app.get("/api/trades")
    def trades(variant: str = "", symbol: str = "", result: str = "") -> JSONResponse:
        s = store()
        q = (
            "SELECT t.* FROM trades t JOIN versions v ON v.variant=t.variant AND "
            "v.version=t.version AND v.active=1 WHERE 1=1"
        )
        args: list[Any] = []
        if variant:
            q += " AND t.variant=?"
            args.append(variant)
        if symbol:
            q += " AND t.symbol=?"
            args.append(symbol)
        if result == "win":
            q += " AND t.status='closed' AND t.net_r > 0"
        elif result == "loss":
            q += " AND t.status='closed' AND t.net_r <= 0"
        elif result == "open":
            q += " AND t.status='open'"
        rows = s.rows(q + " ORDER BY t.entry_ts DESC LIMIT 500", tuple(args))
        vmap = {v["id"]: v for v in variants(s)}
        return _j({
            "trades": [
                {**_trade_row(t), "color": vmap.get(t["variant"], {}).get("color")} for t in rows
            ],
            "variants": list(vmap.values()),
        })  # fmt: skip

    @app.get("/api/trades/{tid}")
    def trade(tid: int) -> JSONResponse:
        s = store()
        t = s.one("SELECT * FROM trades WHERE id=?", (tid,))
        if t is None:
            raise HTTPException(404, "trade not found")
        chart_json = json.loads(t["chart_json"]) if t["chart_json"] else None
        vmap = {v["id"]: v for v in variants(s)}
        return _j({
            **_trade_row(t), "chart": chart_json, "reasons": _reasons(t),
            "has_png": bool(t["chart_png"] and Path(t["chart_png"]).exists()),
            "color": vmap.get(t["variant"], {}).get("color"),
        })  # fmt: skip

    @app.get("/api/trades/{tid}/png")
    def trade_png(tid: int) -> FileResponse:
        s = store()
        t = s.one("SELECT chart_png FROM trades WHERE id=?", (tid,))
        if not t or not t["chart_png"]:
            raise HTTPException(404, "no chart")
        p = Path(t["chart_png"]).resolve()
        root = (data_dir / "paper").resolve()
        if root not in p.parents or not p.exists():
            raise HTTPException(404, "no chart")
        return FileResponse(p, media_type="image/png")

    @app.get("/api/performance")
    def performance() -> JSONResponse:
        s = store()
        rows = ledger_stats(s, cfg)
        for r in rows:
            r["color"] = VARIANT_COLORS.get(r["variant"], "#cbd5e1")
        weeks = sorted({w["week"] for r in rows for w in r["stats"]["weekly"]}, reverse=True)
        return _j({"ledgers": rows, "weeks": weeks})

    @app.get("/api/health")
    def health() -> JSONResponse:
        s = db.get()
        u = shutil.disk_usage(data_dir if data_dir.exists() else Path("."))
        disk = {"pct": 100 * u.used / u.total, "free_gb": u.free / 1e9, "total_gb": u.total / 1e9,
                "limit_pct": cfg.paper.disk_alert_pct}  # fmt: skip
        if s is None:
            return _j({"engine": {"status": "unknown"}, "symbols": [], "disk": disk, "events": [],
                       "notifications": {}, "demo": demo})  # fmt: skip
        sym = []
        clk = _clock(s, demo)
        for x in s.rows("SELECT * FROM symbol_state ORDER BY symbol"):
            lag = (clk - (int(x["last_minute_ts"]) + MIN)) / 1000 if x["last_minute_ts"] else None
            if demo:
                x["updated_ts"] = now_ms() - 3000
            sym.append({**x, "lag_s": lag, "bias": json.loads(x["bias"]) if x["bias"] else None})
        order = {s_: i for i, s_ in enumerate(symbols)}
        sym.sort(key=lambda x: order.get(x["symbol"], 99))
        nots = {r["status"]: r["n"] for r in s.rows(
            "SELECT status, count(*) AS n FROM notifications GROUP BY status")}  # fmt: skip
        last_sent = s.one("SELECT max(sent_ts) AS t FROM notifications WHERE status='sent'")
        return _j({
            "engine": _engine_state(s, cfg, demo), "symbols": sym, "disk": disk,
            "events": s.rows("SELECT * FROM events ORDER BY id DESC LIMIT 60"),
            "notifications": nots, "last_sent_ms": last_sent["t"] if last_sent else None,
            "telegram_web": bool(tg and tg.enabled), "demo": demo,
            "versions": variants(s),
            "stall_minutes": cfg.paper.stall_alert_minutes,
            "verify": _verify_view(vstate, cfg),
        })  # fmt: skip

    @app.get("/api/verify")
    def verify_get() -> JSONResponse:
        return _j(_verify_view(vstate, cfg))

    @app.post("/api/verify/run")
    async def verify_run() -> JSONResponse:
        loop_t = asyncio.get_running_loop().time()
        if not vstate["running"] and loop_t - vstate["last_run"] >= 20:
            await run_verify()
        return _j(_verify_view(vstate, cfg))

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        last = None
        tick = 0
        try:
            while True:
                s = db.get()
                dv = s.data_version() if s else None
                hb = s.get_kv("heartbeat") if s else None
                cur = (dv, hb[1] if hb else None)
                if cur != last or tick % 15 == 0:
                    await sock.send_json(
                        {"type": "update", "dv": dv, "hb": cur[1], "server_ms": now_ms()}
                    )
                    last = cur
                tick += 1
                await asyncio.sleep(1.0)
        except (WebSocketDisconnect, RuntimeError):
            return

    @app.get("/", include_in_schema=False)
    def index() -> Response:
        html = (STATIC / "index.html").read_text()
        return Response(html, media_type="text/html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _verify_view(vstate: dict[str, Any], cfg: AppConfig) -> dict[str, Any]:
    return {
        "result": vstate["result"],
        "running": vstate["running"],
        "every_min": cfg.paper.verify_minutes,
    }


async def _verify_alert(tg: notify.Telegram, prev: dict[str, Any] | None,
                        res: dict[str, Any] | None) -> None:  # fmt: skip
    """Telegram on a pass -> fail transition (and when it passes again)."""
    if res is None:
        return
    was = prev["ok"] if prev else None
    if res["ok"] is False and was is not False:
        bad = [
            f"{x['symbol']}: " + ", ".join(c["name"] for c in x["checks"] if c["ok"] is False)
            + (f" ({x['note']})" if x.get("note") and not x["checks"] else "")
            for x in res["symbols"] if x["ok"] is False
        ]  # fmt: skip
        await tg.send("<b>Cross-check FAILED</b> (engine vs independent recomputation): "
                      + "; ".join(bad)[:600] + ". See Health → Cross-check.")  # fmt: skip
    elif res["ok"] is True and was is False:
        await tg.send("<b>Cross-check passes again</b>.")


def _trade_row(t: dict[str, Any]) -> dict[str, Any]:
    keep = {k: v for k, v in t.items() if k not in ("chart_json",)}
    keep["exit2"] = t["config"].split("|")[-1]
    keep["held_h"] = (
        (int(t["exit_ts"]) if t["exit_ts"] else int(t["last_ts"] or t["entry_ts"]))
        - int(t["entry_ts"])
    ) / 3_600_000
    return keep


async def _watchdog(db: Db, cfg: AppConfig, tg: notify.Telegram) -> None:
    """Alert when the engine heartbeat is stale; alert again when it is back."""
    down_since: int | None = None
    while True:
        await asyncio.sleep(30)
        try:
            s = db.get()
            if s is None:
                continue
            st = _engine_state(s, cfg)
            if st["status"] in ("stale", "stopped") and down_since is None:
                down_since = now_ms()
                ok, err = await tg.send(
                    f"<b>Paper engine {st['status'].upper()}</b>: no heartbeat for "
                    f"{st.get('heartbeat_age_s', 0) / 60:.0f} min (systemd should restart it; "
                    "check `journalctl -u flowlevels-paper`)."
                )
                if not ok:
                    log.warning("watchdog alert failed: %s", err)
            elif st["status"] == "running" and down_since is not None:
                await tg.send(
                    "<b>Paper engine running again</b> after "
                    f"{(now_ms() - down_since) / 60000:.0f} min."
                )
                down_since = None
        except Exception as exc:  # never let the watchdog kill the web process
            log.warning("watchdog error: %s", type(exc).__name__)
