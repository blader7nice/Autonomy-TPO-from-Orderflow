"""Values for the chart's crosshair readout, per bar.

- **TPO levels:** prior-day and prior-week VAH/POC/VAL in force on each bar's UTC day. They
  are computed with the rule code itself (`method4.static_levels`) on the same 1m candles the
  engine uses.
- **Session VWAP:** UTC day, typical price (H+L+C)/3 weighted by 1m volume, as of the bar's
  close. It is display only and not a rule input.

The 4H/daily EMA50 readout comes from the engine's own chart payload (in the browser). The
independent cross-check of all of these is `flowlevels.paper.verify`.
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np
import polars as pl

from flowlevels.config import AppConfig
from flowlevels.strategies import method4 as m4

DAY = m4.DAY
NAMES = ("pdVAH", "pdPOC", "pdVAL", "pwVAH", "pwPOC", "pwVAL")
_lock = threading.Lock()
_levels: dict[tuple[str, int], dict[str, float]] = {}  # completed inputs only, never change


def rule_levels(
    cfg: AppConfig, symbol: str, df: pl.DataFrame, days: list[int]
) -> dict[int, dict[str, float]]:
    """{UTC day: {pdVAH, ..., pwVAL}} by the rule code; days without data are left out."""
    if not df.height:
        return {}
    last = int(df["minute_ts"][-1])
    with _lock:
        todo = sorted({d for d in days if (symbol, d) not in _levels and d <= last})
    if todo:
        lv = m4.static_levels(df, todo, cfg, symbol)
        for d in todo:
            vals: dict[str, float] = {}
            if d in lv.day:
                va = lv.day[d]
                vals.update(pdVAH=va.vah, pdPOC=va.poc, pdVAL=va.val)
            w0 = m4.week_start(d)
            if w0 in lv.week:
                wv = lv.week[w0]
                vals.update(pwVAH=wv.vah, pwPOC=wv.poc, pwVAL=wv.val)
            with _lock:
                _levels[(symbol, d)] = vals
    with _lock:
        return {d: _levels[(symbol, d)] for d in days if (symbol, d) in _levels}


def session_vwap(df: pl.DataFrame, times_ms: list[int]) -> list[float | None]:
    """UTC-session VWAP after the last 1m candle that closes by each time (None if the
    session has no candle yet or no volume)."""
    if not df.height or not times_ms:
        return [None] * len(times_ms)
    lo = (min(times_ms) - 1) // DAY * DAY
    d = df.filter(pl.col("minute_ts") >= lo)
    ts = d["minute_ts"].to_numpy()
    if not len(ts):
        return [None] * len(times_ms)
    v = d["volume"].to_numpy()
    pv = (d["high"].to_numpy() + d["low"].to_numpy() + d["close"].to_numpy()) / 3.0 * v
    day = ts // DAY
    starts = np.flatnonzero(np.r_[True, day[1:] != day[:-1]])
    seg = np.cumsum(np.r_[True, day[1:] != day[:-1]]) - 1
    cpv, cv = np.cumsum(pv), np.cumsum(v)
    base_pv = (cpv[starts] - pv[starts])[seg]
    base_v = (cv[starts] - v[starts])[seg]
    out: list[float | None] = []
    for t in times_ms:
        i = int(np.searchsorted(ts, t - 60_000, side="right")) - 1  # minute closes <= t
        if i < 0 or day[i] != (t - 1) // DAY:
            out.append(None)
            continue
        vol = cv[i] - base_v[i]
        out.append(float((cpv[i] - base_pv[i]) / vol) if vol > 0 else None)
    return out


def for_chart(
    cfg: AppConfig,
    symbol: str,
    step_ms: int,
    bars: list[list[float]],
    end_ms: int,
    df: pl.DataFrame,
) -> dict[str, Any]:
    if not bars or not df.height:
        return {"vwap": [], "levels": {}, "coverage": None}
    cov = int(df["minute_ts"][-1]) + 60_000
    closes = [min(int(b[0]) * 1000 + step_ms, end_ms, cov) for b in bars]
    days = sorted({int(b[0]) * 1000 // DAY * DAY for b in bars})
    lv = rule_levels(cfg, symbol, df, days)
    return {
        "vwap": session_vwap(df, closes),
        "levels": {str(d // 1000): [lv[d].get(n) for n in NAMES] for d in days if d in lv},
        "names": list(NAMES),
        "coverage": cov // 1000,
    }
