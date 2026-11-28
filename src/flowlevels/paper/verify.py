"""Independent cross-check of what the paper engine computed and the dashboard shows.

The rule inputs shown on the chart are recomputed here from the stored 1m candles by a second
implementation, written from the definitions (DECISIONS D-016, ITERATION-5-PREREG):
- OHLC bars per timeframe
- 4H and daily EMA50 (alpha = 2/(n+1), seeded with the first close)
- prior-day and prior-week TPO VAH/POC/VAL (30-min periods, 70 % value area)
- the bias

This module deliberately imports nothing from `flowlevels.strategies` or `flowlevels.levels`.
The stored 1m candles are checked against Binance REST.

Used by `flowlevels verify` and by the Health page (via the web process). It is read-only and
never writes the ledger or the live kline store.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import polars as pl

from flowlevels.config import AppConfig
from flowlevels.data import klines
from flowlevels.venues.binance import parse_rest_klines

MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR
WEEK = 7 * DAY
TF_MS = {"15m": 15 * MIN, "30m": 30 * MIN, "1h": HOUR, "4h": 4 * HOUR}
SCHEMA = {
    "minute_ts": pl.Int64,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
}
LEVEL_NAMES = ("pdVAH", "pdPOC", "pdVAL", "pwVAH", "pwPOC", "pwVAL")

FetchSync = Callable[[str, int | None, int], pl.DataFrame]


def rest_fetcher(rest_url: str, timeout_s: float = 6.0) -> FetchSync:
    """Blocking Binance REST `/fapi/v1/klines` 1m (oldest first, forming row included)."""
    client = httpx.Client(timeout=timeout_s)
    base = rest_url.rstrip("/")

    def fetch(symbol: str, start_ms: int | None, limit: int) -> pl.DataFrame:
        params: dict[str, Any] = {"symbol": symbol, "interval": "1m", "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        r = client.get(f"{base}/fapi/v1/klines", params=params)
        r.raise_for_status()
        return parse_rest_klines(r.json())

    return fetch


# ---------------------------------------------------------------------------- candles


class Candles:
    """1m candles as the engine sees them: the research store, the live store (flushed by the
    engine every `paper.flush_minutes`), and a REST tail for the minutes that are not flushed
    yet. As in the engine, the newest REST row is dropped (forming or possibly incomplete).
    Cached per symbol; the tail is refreshed at most every `tail_s` seconds. Thread-safe."""

    def __init__(self, data_dir: Path, fetch: FetchSync | None, tail_s: float = 50.0) -> None:
        self.roots = (data_dir / "klines", data_dir / "paper" / "klines")
        self.fetch = fetch
        self.tail_s = tail_s
        self._lock = threading.Lock()
        self._disk: dict[str, tuple[Any, int, pl.DataFrame]] = {}
        self._tail: dict[str, tuple[float, pl.DataFrame]] = {}
        self.tail_error: dict[str, str] = {}

    def _signature(self, symbol: str) -> tuple[tuple[str, int, int], ...]:
        out = []
        for root in self.roots:
            for p in sorted((root / symbol).glob("*.parquet")):
                st = p.stat()
                out.append((str(p), st.st_mtime_ns, st.st_size))
        return tuple(out)

    def disk(self, symbol: str, anchor: int) -> pl.DataFrame:
        sig = self._signature(symbol)
        with self._lock:
            hit = self._disk.get(symbol)
            if hit and hit[0] == sig and hit[1] == anchor:
                return hit[2]
        res = klines.load(self.roots[0], symbol, anchor, 2**62).cast(SCHEMA)  # type: ignore[arg-type]
        live = klines.load(self.roots[1], symbol, anchor, 2**62).cast(SCHEMA)  # type: ignore[arg-type]
        df = pl.concat([res, live]).unique("minute_ts", keep="first").sort("minute_ts")
        with self._lock:
            self._disk[symbol] = (sig, anchor, df)
        return df

    def get(self, symbol: str, anchor: int, fresh: bool = False) -> tuple[pl.DataFrame, int]:
        """(candles from `anchor` incl. the REST tail, last minute held on disk). `fresh`
        refetches the tail now instead of using one up to `tail_s` old."""
        df = self.disk(symbol, anchor)
        disk_last = int(df["minute_ts"][-1]) if df.height else anchor - MIN
        tail = self._get_tail(symbol, disk_last, fresh)
        if tail.height:
            df = pl.concat([df, tail.filter(pl.col("minute_ts") > disk_last)])
        return df, disk_last

    def _get_tail(self, symbol: str, disk_last: int, fresh: bool = False) -> pl.DataFrame:
        if self.fetch is None:
            return pl.DataFrame(schema=SCHEMA)
        now = time.monotonic()
        with self._lock:
            hit = self._tail.get(symbol)
        if hit and not fresh and now - hit[0] < self.tail_s:
            return hit[1]
        try:
            raw = self.fetch(symbol, disk_last + MIN, 99)  # REST weight 1
            tail = raw.head(max(raw.height - 1, 0)).cast(SCHEMA)  # type: ignore[arg-type]
            self.tail_error.pop(symbol, None)
        except Exception as exc:  # keep the previous tail; the check reports the gap
            self.tail_error[symbol] = f"{type(exc).__name__}: {exc}"[:200]
            tail = hit[1] if hit else pl.DataFrame(schema=SCHEMA)
        with self._lock:
            self._tail[symbol] = (now, tail)
        return tail


# ---------------------------------------------------------------------------- reference math


def agg_bars(
    ts: np.ndarray, o: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray, step: int
) -> dict[int, tuple[float, float, float, float]]:
    """{bar open: (open, high, low, close)} from sorted 1m rows (first/max/min/last)."""
    if len(ts) == 0:
        return {}
    keys = ts // step * step
    starts = np.flatnonzero(np.r_[True, keys[1:] != keys[:-1]])
    ends = np.r_[starts[1:], len(ts)]
    hi = np.maximum.reduceat(h, starts)
    low = np.minimum.reduceat(lo, starts)
    return {
        int(keys[a]): (float(o[a]), float(hi[i]), float(low[i]), float(c[b - 1]))
        for i, (a, b) in enumerate(zip(starts, ends, strict=True))
    }


def ema_ref(x: list[float], n: int) -> list[float]:
    """EMA seeded with the first value: e_t = (1 - a) e_(t-1) + a x_t, a = 2 / (n + 1)."""
    a = 2.0 / (n + 1)
    out: list[float] = []
    e = 0.0
    for i, v in enumerate(x):
        e = v if i == 0 else (1.0 - a) * e + a * v
        out.append(e)
    return out


def tpo_value_area(
    ts: np.ndarray,
    h: np.ndarray,
    lo: np.ndarray,
    period_ms: int,
    row_size: float,
    tick: float,
    pct: float,
) -> tuple[float, float, float] | None:
    """(VAH, POC, VAL) of a TPO profile.

    - Every period adds 1 to each row from its low to its high. A row index is
      round(price / tick) // round(row_size / tick).
    - POC = the row with the most TPOs. Ties go to the row closest to the middle row, then
      the lower one.
    - Value area: start at the POC and add the bigger of the next two rows above or below
      (both on a tie) until the area reaches pct of all TPOs.
    - POC price = row mid; VAH = top edge; VAL = bottom edge.
    """
    if len(ts) == 0:
        return None
    per_row = round(row_size / tick)
    periods: dict[int, list[float]] = {}
    for t, hi, low in zip(ts.tolist(), h.tolist(), lo.tolist(), strict=True):
        k = t // period_ms
        p = periods.get(k)
        if p is None:
            periods[k] = [hi, low]
        else:
            p[0] = max(p[0], hi)
            p[1] = min(p[1], low)
    counts: dict[int, int] = {}
    for hi, low in periods.values():
        r0 = round(low / tick) // per_row
        r1 = round(hi / tick) // per_row
        for r in range(r0, r1 + 1):
            counts[r] = counts.get(r, 0) + 1
    base = min(counts)
    rows = [counts.get(r, 0) for r in range(base, max(counts) + 1)]
    n = len(rows)
    total = sum(rows)
    if total <= 0:
        return None
    best = max(rows)
    mid = (n - 1) / 2.0
    poc = min((i for i in range(n) if rows[i] == best), key=lambda i: (abs(i - mid), i))
    a = b = poc
    inside = rows[poc]
    while inside < pct * total and (a > 0 or b < n - 1):
        k_up, k_dn = min(2, n - 1 - b), min(2, a)
        up = sum(rows[b + 1 : b + 1 + k_up]) if k_up else -1
        dn = sum(rows[a - k_dn : a]) if k_dn else -1
        if up >= dn:
            b += k_up
            inside += max(up, 0)
        if dn >= up:
            a -= k_dn
            inside += max(dn, 0)
    return ((base + b + 1) * row_size, (base + poc + 0.5) * row_size, (base + a) * row_size)


def monday_of(day_ms: int) -> int:
    d = dt.datetime.fromtimestamp(day_ms / 1000, dt.UTC)
    return day_ms - d.weekday() * DAY


def ref_levels(df: pl.DataFrame, d0: int, cfg: AppConfig, symbol: str) -> dict[str, float]:
    """Prior-day and prior-week TPO levels in force on UTC day d0 (as the rules define them:
    the day needs >= 1380 of 1440 minutes and the first minute of d0; the week needs
    >= 7 x 1380 minutes)."""
    sc, lc = cfg.symbol(symbol), cfg.levels
    ts = df["minute_ts"].to_numpy()
    h, lo = df["high"].to_numpy(), df["low"].to_numpy()
    out: dict[str, float] = {}

    def va(a_ms: int, b_ms: int, need: int) -> tuple[float, float, float] | None:
        a, b = np.searchsorted(ts, [a_ms, b_ms])
        if b - a < need:
            return None
        return tpo_value_area(
            ts[a:b],
            h[a:b],
            lo[a:b],
            lc.tpo_period_minutes * MIN,
            sc.tpo_tick,
            sc.exchange_tick,
            lc.value_area_pct,
        )

    i0 = int(np.searchsorted(ts, d0))
    if i0 < len(ts) and int(ts[i0]) == d0:
        v = va(d0 - DAY, d0, 1380)
        if v:
            out.update(pdVAH=v[0], pdPOC=v[1], pdVAL=v[2])
    w0 = monday_of(d0)
    v = va(w0 - WEEK, w0, 7 * 1380)
    if v:
        out.update(pwVAH=v[0], pwPOC=v[1], pwVAL=v[2])
    return out


# ---------------------------------------------------------------------------- checks


def _rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(a), abs(b), 1e-12)


def _check(name: str, n: int, bad: list[str], max_rel: float, detail: str = "") -> dict[str, Any]:
    ok: bool | None = None if n == 0 else not bad
    msg = detail or ("; ".join(bad[:3]) if bad else "")
    return {"name": name, "ok": ok, "n": n, "max_rel": max_rel, "detail": msg}


def check_symbol(
    cfg: AppConfig,
    symbol: str,
    payloads: dict[str, dict[str, Any]],
    df: pl.DataFrame,
    disk: pl.DataFrame,
    rest: pl.DataFrame | None,
    rule_levels: dict[int, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Compare the engine's chart payloads for one symbol with the reference values.

    - df: candles from the anchor, with the REST tail.
    - disk: the stored candles only, without the tail.
    - rest: fresh REST candles that overlap `disk`.
    - rule_levels: TPO levels computed by the rule code (e.g. the dashboard readout), keyed by
      UTC day.
    """
    tol = cfg.paper.verify_rel_tol
    i5 = cfg.iteration5
    ov = payloads.get("overlay")
    checks: list[dict[str, Any]] = []
    if ov is None:
        return {"symbol": symbol, "ok": None, "checks": [], "note": "no chart payload yet"}
    end = int(ov["end"]) * 1000
    cov = int(df["minute_ts"][-1]) + MIN if df.height else 0
    upto = df.filter(pl.col("minute_ts") < end)
    ts = upto["minute_ts"].to_numpy()
    o, h, lo, c = (upto[k].to_numpy() for k in ("open", "high", "low", "close"))

    # 1. stored candles vs Binance REST
    if rest is not None:
        bad: list[str] = []
        j = rest.join(disk, on="minute_ts", how="inner", suffix="_s")
        mx = 0.0
        for row in j.iter_rows(named=True):
            for col in ("open", "high", "low", "close", "volume"):
                if row[col] != row[f"{col}_s"]:
                    mx = max(mx, _rel(row[col], row[f"{col}_s"]))
                    bad.append(
                        f"{_utc(row['minute_ts'])} {col} {row[f'{col}_s']} != REST {row[col]}"
                    )
        checks.append(_check("candles_vs_rest", j.height, bad, mx))

    # 2. OHLC bars per timeframe (bars that end by min(end, coverage))
    n = 0
    bad = []
    mx = 0.0
    for tf, step in TF_MS.items():
        p = payloads.get(tf)
        if not p:
            continue
        ref = agg_bars(ts, o, h, lo, c, step)
        limit = end if cov >= end else cov // step * step
        for b in p["bars"]:
            t = int(b[0]) * 1000
            if min(t + step, end) > limit or t not in ref:
                continue
            n += 1
            for k, (x, y) in enumerate(zip(b[1:], ref[t], strict=True)):
                if x != y:
                    mx = max(mx, _rel(x, y))
                    bad.append(f"{tf} {_utc(t)} {'OHLC'[k]} {x} != {y}")
    checks.append(_check("bars", n, bad, mx))

    # 3. EMA50 4H and daily: value in force at every payload point
    closed_close: dict[str, tuple[list[int], list[float]]] = {}
    for name, step, label in (("ema4", 4 * HOUR, "ema50_4h"), ("emad", DAY, "ema50_1d")):
        bars = agg_bars(ts, o, h, lo, c, step)
        keys = sorted(k for k in bars if k + step <= end)
        e = ema_ref([bars[k][3] for k in keys], i5.ema_period)
        cts = [k + step for k in keys]
        closed_close[name] = (cts, e)
        n, bad, mx = 0, [], 0.0
        for tf in TF_MS:
            for t_s, v in (payloads.get(tf) or {}).get(name, []):
                t = int(t_s) * 1000
                if t > cov:
                    continue
                k = int(np.searchsorted(cts, t, side="right")) - 1
                if k < 0:
                    continue
                n += 1
                r = _rel(float(v), e[k])
                mx = max(mx, r)
                if r > tol:
                    bad.append(f"{tf} {_utc(t)} {v:.10g} != {e[k]:.10g}")
        checks.append(_check(label, n, bad, mx))

    # 4. TPO levels in force now
    d0 = (end - 1) // DAY * DAY
    ref_now = ref_levels(df, d0, cfg, symbol)
    n, bad, mx = 0, [], 0.0
    shown = {lv["name"]: float(lv["price"]) for lv in ov.get("levels", [])}
    for name in LEVEL_NAMES:
        if name not in shown and name not in ref_now:
            continue
        n += 1
        if name not in shown or name not in ref_now:
            bad.append(f"{name}: engine {shown.get(name)} vs reference {ref_now.get(name)}")
            continue
        r = _rel(shown[name], ref_now[name])
        mx = max(mx, r)
        if r > tol:
            bad.append(f"{name} {shown[name]} != {ref_now[name]}")
    checks.append(_check("tpo_now", n, bad, mx))

    # 5. TPO levels per day used by the dashboard readout (rule code) vs reference
    if rule_levels:
        n, bad, mx = 0, [], 0.0
        for day, vals in sorted(rule_levels.items()):
            ref_d = ref_levels(df, day, cfg, symbol)
            for name in LEVEL_NAMES:
                xv, yv = vals.get(name), ref_d.get(name)
                if xv is None and yv is None:
                    continue
                n += 1
                if xv is None or yv is None:
                    bad.append(f"{_utc(day)[:10]} {name}: rules {xv} vs reference {yv}")
                    continue
                r = _rel(xv, yv)
                mx = max(mx, r)
                if r > tol:
                    bad.append(f"{_utc(day)[:10]} {name} {xv} != {yv}")
        checks.append(_check("tpo_history", n, bad, mx))

    # 6. bias at the engine's last computation
    b = ov.get("bias") or {}
    if b.get("ready") and cov >= end and len(ts):
        c4, e4 = closed_close["ema4"]
        cd, ed = closed_close["emad"]
        bars4 = agg_bars(ts, o, h, lo, c, 4 * HOUR)
        barsd = agg_bars(ts, o, h, lo, c, DAY)
        k4, kd = len(c4) - 1, len(cd) - 1
        close4 = bars4[c4[k4] - 4 * HOUR][3]
        closed = barsd[cd[kd] - DAY][3]
        sb = i5.ema_slope_bars
        slope = e4[k4] - e4[k4 - sb] if k4 >= sb else float("nan")
        b1 = 1 if close4 > e4[k4] and slope > 0 else -1 if close4 < e4[k4] and slope < 0 else 0
        last = float(c[-1])
        d = 0
        for s in (1, -1):
            if b1 == s and s * (last - e4[k4]) > 0 and s * (closed - ed[kd]) > 0:
                d = s
        bad = []
        mx = 0.0
        for key, ref_v in (("ema4", e4[k4]), ("emad", ed[kd]), ("close_d", closed), ("last", last)):
            r = _rel(float(b[key]), ref_v)
            mx = max(mx, r)
            if r > tol:
                bad.append(f"{key} {b[key]} != {ref_v}")
        if int(b["b1"]) != b1:
            bad.append(f"b1 {b['b1']} != {b1}")
        if int(b["dir"]) != d:
            bad.append(f"dir {b['dir']} != {d}")
        checks.append(_check("bias", 1, bad, mx))
    else:
        checks.append(_check("bias", 0, [], 0.0, "waiting for candles up to the engine's end"))

    oks = [x["ok"] for x in checks if x["ok"] is not None]
    return {
        "symbol": symbol,
        "end": end,
        "coverage": cov,
        "ok": (all(oks) if oks else None),
        "checks": checks,
    }


def _utc(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M")


def load_payloads(store: Any, symbol: str) -> dict[str, dict[str, Any]]:
    import json

    rows = store.rows("SELECT tf, payload FROM chart_cache WHERE symbol=?", (symbol,))
    return {r["tf"]: json.loads(r["payload"]) for r in rows}


def anchor_of(store: Any) -> int | None:
    r = store.one("SELECT min(anchor) AS a FROM versions WHERE active=1")
    return int(r["a"]) if r and r["a"] is not None else None


def run(
    cfg: AppConfig,
    store: Any,
    candles: Candles,
    symbols: list[str],
    rest: bool = True,
    history_days: int | None = None,
) -> dict[str, Any]:
    """Cross-check every symbol. `rest` also compares the newest stored minutes with Binance.
    The TPO history check compares the rule code's per-day levels (the dashboard readout) with
    the reference for the last `history_days` days."""
    from flowlevels.web import readout  # rule code, the thing being checked

    t0 = time.perf_counter()
    started = int(time.time() * 1000)
    hd = cfg.paper.verify_history_days if history_days is None else history_days
    anchor = anchor_of(store)
    out: list[dict[str, Any]] = []
    for s in symbols:
        try:
            if anchor is None:
                out.append({"symbol": s, "ok": None, "checks": [], "note": "no ledger yet"})
                continue
            payloads = load_payloads(store, s)
            df, disk_last = candles.get(s, anchor, fresh=True)
            disk = candles.disk(s, anchor)
            rest_df = None
            if rest and candles.fetch is not None and disk.height:
                rest_df = candles.fetch(s, disk_last - 59 * MIN, 60)  # REST weight 1
            ov = payloads.get("overlay")
            rule = None
            if ov is not None and hd > 0:
                d_end = (int(ov["end"]) * 1000 - 1) // DAY * DAY
                days = [d_end - i * DAY for i in range(hd)]
                rule = readout.rule_levels(cfg, s, df, days)
            res = check_symbol(cfg, s, payloads, df, disk, rest_df, rule)
            if s in candles.tail_error:
                res["note"] = f"REST tail: {candles.tail_error[s]}"
            out.append(res)
        except Exception as exc:
            out.append(
                {
                    "symbol": s,
                    "ok": False,
                    "checks": [],
                    "note": f"{type(exc).__name__}: {exc}"[:300],
                }
            )
    oks = [x["ok"] for x in out if x["ok"] is not None]
    return {
        "ts": started,
        "duration_s": round(time.perf_counter() - t0, 2),
        "ok": (all(oks) if oks else None),
        "rest": rest,
        "rel_tol": cfg.paper.verify_rel_tol,
        "symbols": out,
    }
