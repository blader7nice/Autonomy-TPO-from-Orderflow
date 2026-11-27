"""SQLite store of the paper test (DECISIONS PAPER-PLAN).

One writer (the engine) and any number of read-only readers (the dashboard). WAL mode so
readers never block the writer. All timestamps are epoch milliseconds (UTC).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    variant TEXT NOT NULL,
    version TEXT NOT NULL,
    key TEXT NOT NULL,
    spec TEXT NOT NULL,
    paper_start INTEGER NOT NULL,
    run_start INTEGER NOT NULL,
    anchor INTEGER NOT NULL,
    created_ts INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (variant, version)
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant TEXT NOT NULL,
    version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    config TEXT NOT NULL,
    trigger TEXT NOT NULL,
    direction INTEGER NOT NULL,
    entry_ts INTEGER NOT NULL,
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    r_unit REAL NOT NULL,
    far REAL,
    stop_anchor REAL,
    atr1h REAL,
    tp1 REAL NOT NULL,
    tp2 REAL,
    tp1_r REAL,
    ema4 REAL,
    emad REAL,
    close_d REAL,
    pois TEXT,
    a_ts INTEGER,
    b_ts INTEGER,
    trig_step INTEGER,
    extreme REAL,
    swing REAL,
    status TEXT NOT NULL,              -- open | closed
    tp1_hit INTEGER NOT NULL DEFAULT 0,
    x1_ts INTEGER,
    x1_px REAL,
    x2_ts INTEGER,
    x2_px REAL,
    exit_ts INTEGER,
    exit_px REAL,
    exit_kind TEXT,
    gross_r REAL,
    net_r REAL,                        -- default costs; for open trades: if closed at last_px
    net_bps REAL,
    cost_r REAL,
    net_r_maker REAL,
    net_r_2tick REAL,
    net_r_4tick REAL,
    mfe_r REAL,
    mae_r REAL,
    last_px REAL,
    last_ts INTEGER,
    detected_ts INTEGER NOT NULL,      -- exchange time (last closed minute) at detection
    detected_late INTEGER NOT NULL DEFAULT 0,
    chart_png TEXT,
    chart_json TEXT,
    updated_ts INTEGER NOT NULL,
    UNIQUE (variant, version, symbol, entry_ts)
);
CREATE INDEX IF NOT EXISTS trades_status ON trades (status);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant TEXT NOT NULL,
    version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    trigger TEXT NOT NULL,
    direction INTEGER NOT NULL,
    decision_ts INTEGER NOT NULL,
    a_ts INTEGER,
    b_ts INTEGER,
    far REAL,
    pois TEXT,
    outcome TEXT NOT NULL,
    detected_ts INTEGER NOT NULL,
    UNIQUE (variant, version, symbol, decision_ts, direction)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,               -- wall clock (operational)
    level TEXT NOT NULL,               -- info | warning | error
    kind TEXT NOT NULL,
    symbol TEXT,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    photo TEXT,
    status TEXT NOT NULL,              -- pending | sent | failed | disabled
    attempts INTEGER NOT NULL DEFAULT 0,
    sent_ts INTEGER,
    last_error TEXT,
    dedupe TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS symbol_state (
    symbol TEXT PRIMARY KEY,
    last_minute_ts INTEGER,            -- open time of the last closed 1m kline
    last_close REAL,
    bias TEXT,                         -- JSON
    compute_ms REAL,
    updated_ts INTEGER NOT NULL        -- wall clock
);
CREATE TABLE IF NOT EXISTS chart_cache (
    symbol TEXT NOT NULL,
    tf TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_ts INTEGER NOT NULL,
    PRIMARY KEY (symbol, tf)
);
"""

TRADE_COLS = [
    "variant",
    "version",
    "symbol",
    "config",
    "trigger",
    "direction",
    "entry_ts",
    "entry",
    "stop",
    "r_unit",
    "far",
    "stop_anchor",
    "atr1h",
    "tp1",
    "tp2",
    "tp1_r",
    "ema4",
    "emad",
    "close_d",
    "pois",
    "a_ts",
    "b_ts",
    "trig_step",
    "extreme",
    "swing",
    "status",
    "tp1_hit",
    "x1_ts",
    "x1_px",
    "x2_ts",
    "x2_px",
    "exit_ts",
    "exit_px",
    "exit_kind",
    "gross_r",
    "net_r",
    "net_bps",
    "cost_r",
    "net_r_maker",
    "net_r_2tick",
    "net_r_4tick",
    "mfe_r",
    "mae_r",
    "last_px",
    "last_ts",
    "detected_ts",
    "detected_late",
    "chart_png",
    "chart_json",
    "updated_ts",
]


def now_ms() -> int:
    """Wall clock in ms: operational use only (heartbeat, lag, event times), never in rules."""
    return int(time.time() * 1000)


def _deep_clean(x: Any) -> Any:
    if isinstance(x, float):
        return x if x == x and abs(x) != float("inf") else None
    if isinstance(x, dict):
        return {k: _deep_clean(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_deep_clean(v) for v in x]
    return x


def _clean(v: Any) -> Any:
    if isinstance(v, float) and v != v:  # NaN -> NULL
        return None
    return v


class Store:
    def __init__(self, path: Path, readonly: bool = False) -> None:
        self.path = path
        self.readonly = readonly
        if readonly:
            self.db = sqlite3.connect(
                f"file:{path}?mode=ro", uri=True, check_same_thread=False, timeout=5
            )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(SCHEMA)
            self.set_kv("schema_version", str(SCHEMA_VERSION))
        self.db.row_factory = sqlite3.Row

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.db:
            yield self.db

    # ------------------------------------------------------------------ generic
    def rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        r = self.db.execute(sql, args).fetchone()
        return dict(r) if r is not None else None

    def data_version(self) -> int:
        return int(self.db.execute("PRAGMA data_version").fetchone()[0])

    # ------------------------------------------------------------------ kv
    def set_kv(self, key: str, value: Any) -> None:
        v = value if isinstance(value, str) else json.dumps(value)
        with self.tx() as db:
            db.execute(
                "INSERT INTO kv(key, value, updated_ts) VALUES(?,?,?) ON CONFLICT(key) DO "
                "UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
                (key, v, now_ms()),
            )

    def get_kv(self, key: str) -> tuple[str, int] | None:
        r = self.db.execute("SELECT value, updated_ts FROM kv WHERE key=?", (key,)).fetchone()
        return (r[0], int(r[1])) if r else None

    # ------------------------------------------------------------------ versions
    def versions(self, active_only: bool = True) -> list[dict[str, Any]]:
        q = "SELECT * FROM versions" + (" WHERE active=1" if active_only else "")
        return self.rows(q + " ORDER BY variant, created_ts")

    def ensure_version(
        self,
        variant: str,
        version: str,
        key: str,
        spec: dict[str, Any],
        paper_start: int,
        run_start: int,
        anchor: int,
    ) -> dict[str, Any]:
        """Return the stored version row, creating it (and retiring older versions of the
        variant) if this rule version is new."""
        r = self.one("SELECT * FROM versions WHERE variant=? AND version=?", (variant, version))
        if r is not None:
            if not r["active"]:
                with self.tx() as db:
                    db.execute("UPDATE versions SET active=0 WHERE variant=?", (variant,))
                    db.execute(
                        "UPDATE versions SET active=1 WHERE variant=? AND version=?",
                        (variant, version),
                    )
                r["active"] = 1
            return r
        with self.tx() as db:
            db.execute("UPDATE versions SET active=0 WHERE variant=?", (variant,))
            db.execute(
                "INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,1)",
                (variant, version, key, json.dumps(spec), paper_start, run_start, anchor, now_ms()),
            )
        out = self.one("SELECT * FROM versions WHERE variant=? AND version=?", (variant, version))
        assert out is not None
        return out

    # ------------------------------------------------------------------ trades
    def trade(
        self, variant: str, version: str, symbol: str, entry_ts: int
    ) -> dict[str, Any] | None:
        return self.one(
            "SELECT * FROM trades WHERE variant=? AND version=? AND symbol=? AND entry_ts=?",
            (variant, version, symbol, entry_ts),
        )

    def upsert_trade(self, t: dict[str, Any]) -> int:
        cols = [c for c in TRADE_COLS if c in t]
        vals = tuple(_clean(t[c]) for c in cols)
        upd = ", ".join(
            f"{c}=excluded.{c}" for c in cols if c not in ("detected_ts", "detected_late")
        )
        with self.tx() as db:
            db.execute(
                f"INSERT INTO trades({', '.join(cols)}) VALUES({', '.join('?' * len(cols))}) "
                f"ON CONFLICT(variant, version, symbol, entry_ts) DO UPDATE SET {upd}",
                vals,
            )
        r = self.trade(t["variant"], t["version"], t["symbol"], t["entry_ts"])
        assert r is not None
        return int(r["id"])

    def open_trades(self) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT t.* FROM trades t JOIN versions v ON v.variant=t.variant AND "
            "v.version=t.version AND v.active=1 WHERE t.status='open' ORDER BY t.entry_ts"
        )

    # ------------------------------------------------------------------ signals
    def add_signals(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        cols = [
            "variant",
            "version",
            "symbol",
            "trigger",
            "direction",
            "decision_ts",
            "a_ts",
            "b_ts",
            "far",
            "pois",
            "outcome",
            "detected_ts",
        ]
        with self.tx() as db:
            before = db.total_changes
            db.executemany(
                f"INSERT OR IGNORE INTO signals({', '.join(cols)}) "
                f"VALUES({', '.join('?' * len(cols))})",
                [tuple(_clean(r[c]) for c in cols) for r in rows],
            )
            return db.total_changes - before

    # ------------------------------------------------------------------ events / notifications
    def event(self, level: str, kind: str, message: str, symbol: str | None = None) -> None:
        with self.tx() as db:
            db.execute(
                "INSERT INTO events(ts, level, kind, symbol, message) VALUES(?,?,?,?,?)",
                (now_ms(), level, kind, symbol, message),
            )

    def notify(
        self,
        kind: str,
        text: str,
        photo: str | None = None,
        dedupe: str | None = None,
        enabled: bool = True,
    ) -> bool:
        """Queue a notification; returns False if the dedupe key was already queued."""
        with self.tx() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO notifications"
                "(created_ts, kind, text, photo, status, dedupe) VALUES(?,?,?,?,?,?)",
                (now_ms(), kind, text, photo, "pending" if enabled else "disabled", dedupe),
            )
            return cur.rowcount > 0

    def pending_notifications(self, max_attempts: int) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM notifications WHERE status='pending' AND attempts < ? ORDER BY id",
            (max_attempts,),
        )

    def mark_notification(self, nid: int, ok: bool, error: str | None, max_attempts: int) -> None:
        with self.tx() as db:
            if ok:
                db.execute(
                    "UPDATE notifications SET status='sent', sent_ts=?, attempts=attempts+1, "
                    "last_error=NULL WHERE id=?",
                    (now_ms(), nid),
                )
            else:
                db.execute(
                    "UPDATE notifications SET attempts=attempts+1, last_error=?, status=CASE WHEN "
                    "attempts+1 >= ? THEN 'failed' ELSE 'pending' END WHERE id=?",
                    (error, max_attempts, nid),
                )

    # ------------------------------------------------------------------ symbol state / charts
    def set_symbol_state(
        self,
        symbol: str,
        last_minute_ts: int | None,
        last_close: float | None,
        bias: dict[str, Any] | None,
        compute_ms: float | None,
    ) -> None:
        with self.tx() as db:
            db.execute(
                "INSERT INTO symbol_state VALUES(?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET "
                "last_minute_ts=excluded.last_minute_ts, last_close=excluded.last_close, "
                "bias=COALESCE(excluded.bias, symbol_state.bias), "
                "compute_ms=COALESCE(excluded.compute_ms, symbol_state.compute_ms), "
                "updated_ts=excluded.updated_ts",
                (
                    symbol,
                    last_minute_ts,
                    _clean(last_close),
                    json.dumps(_deep_clean(bias)) if bias is not None else None,
                    compute_ms,
                    now_ms(),
                ),
            )

    def set_charts(self, symbol: str, payloads: dict[str, dict[str, Any]]) -> None:
        t = now_ms()
        with self.tx() as db:
            db.executemany(
                "INSERT INTO chart_cache VALUES(?,?,?,?) ON CONFLICT(symbol, tf) DO UPDATE SET "
                "payload=excluded.payload, updated_ts=excluded.updated_ts",
                [
                    (
                        symbol,
                        tf,
                        json.dumps(_deep_clean(p), separators=(",", ":"), allow_nan=False),
                        t,
                    )
                    for tf, p in payloads.items()
                ],
            )
