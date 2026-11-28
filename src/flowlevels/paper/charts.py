"""Chart payloads for the dashboard (lightweight-charts: times in seconds) and the bias state.

Built by the engine from the same strategy `Context` the rules used, so the dashboard shows
exactly what the rules saw: bars, 4H/daily EMA50 at closed bars, prior-day/week TPO levels,
POIs and their state.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from flowlevels.strategies import method4 as m4
from flowlevels.strategies import method5 as m5

TFS = {"15m": m4.M15, "30m": m4.M30, "1h": m4.H1, "4h": m4.H4}
NEVER = 2**62


def _num(x: float) -> float | None:
    return float(x) if np.isfinite(x) else None


def bars_json(b: m4.Bars, end: int, n: int) -> list[list[float]]:
    k = max(0, len(b.ts) - n)
    return [
        [int(t) // 1000, float(o), float(h), float(lo), float(c)]
        for t, o, h, lo, c in zip(b.ts[k:], b.o[k:], b.h[k:], b.l[k:], b.c[k:], strict=True)
    ]


def ema_closed(b: m4.Bars, e: np.ndarray, end: int, t_from: int) -> list[list[float]]:
    """EMA value at each *closed* bar's close time (the value the rules see from then on),
    plus the value in force at t_from."""
    ct = b.ts + b.step
    closed = ct <= end
    sel = closed & (ct >= t_from)
    k0 = int(np.searchsorted(ct, t_from, side="right")) - 1
    out = []
    if k0 >= 0 and closed[k0]:
        out.append([t_from // 1000, float(e[k0])])
    out += [[int(t) // 1000, float(v)] for t, v in zip(ct[sel], e[sel], strict=True) if t > t_from]
    return out


def bias_state(ctx: m5.Context) -> dict[str, Any]:
    """The rule's three bias conditions evaluated now (last close stands in for the entry)."""
    end = ctx.end
    k4, kd = ctx.b4.last_closed(end), ctx.bd.last_closed(end)
    last = float(ctx.path.close[-1])
    if k4 < 0 or kd < 0:
        return {"dir": 0, "ready": False}
    b1 = int(ctx.bias4[k4])
    e4, ed, cd = float(ctx.ema4[k4]), float(ctx.emad[kd]), float(ctx.bd.c[kd])
    out: dict[str, Any] = {
        "ready": True,
        "b1": b1,
        "ema4": e4,
        "emad": ed,
        "close_d": cd,
        "last": last,
        "dir": 0,
    }
    for d in (1, -1):
        if b1 == d and d * (last - e4) > 0 and d * (cd - ed) > 0:
            out["dir"] = d
    out["checks"] = [
        {"name": "4H EMA50 + slope (b1)", "long": b1 == 1, "short": b1 == -1},
        {"name": "price vs 4H EMA50", "long": last > e4, "short": last < e4},
        {"name": "daily close vs daily EMA50", "long": cd > ed, "short": cd < ed},
    ]
    return out


def poi_state(ctx: m5.Context, p: m4.POI) -> str:
    """fresh = untested; testing = first touched within the last T2 window (30m bars)."""
    if p.first_touch_ts >= NEVER:
        return "fresh"
    if ctx.end - p.first_touch_ts <= 3 * m4.M30:
        return "testing"
    return "used"


def overlay(ctx: m5.Context, cfg_bars: int) -> dict[str, Any]:
    end = ctx.end
    d0 = (end - 1) // m4.DAY * m4.DAY
    levels = []
    if d0 in ctx.lv.day:
        va = ctx.lv.day[d0]
        levels += [
            {"name": "pdVAH", "price": va.vah},
            {"name": "pdPOC", "price": va.poc},
            {"name": "pdVAL", "price": va.val},
        ]
    w0 = m4.week_start(d0)
    if w0 in ctx.lv.week:
        wv = ctx.lv.week[w0]
        levels += [
            {"name": "pwVAH", "price": wv.vah},
            {"name": "pwPOC", "price": wv.poc},
            {"name": "pwVAL", "price": wv.val},
        ]
    pois: list[dict[str, Any]] = []
    for p in ctx.pois + ctx.zs:
        if not (p.start <= end - 1 < p.end):
            continue
        st = poi_state(ctx, p)
        if st == "used":
            continue
        pois.append(
            {
                "kind": p.kind,
                "dir": p.direction,
                "near": p.near,
                "far": p.far,
                "from": p.start // 1000,
                "to": p.end // 1000,
                "state": st,
            }
        )
    last = float(ctx.path.close[-1])
    pois.sort(key=lambda q: abs(float(q["near"]) - last))
    return {
        "end": end // 1000,
        "last_close": last,
        "bias": bias_state(ctx),
        "levels": levels,
        "pois": pois[:40],
    }


def symbol_payloads(ctx: m5.Context, n_bars: int) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    built = {"15m": ctx.b15, "30m": ctx.b30, "4h": ctx.b4}
    for tf, step in TFS.items():
        b = built.get(tf) or m4.make_bars(ctx.m.tail(n_bars * 60 + 60), step)
        bars = bars_json(b, ctx.end, n_bars)
        t_from = int(bars[0][0]) * 1000 if bars else ctx.end
        out[tf] = {
            "tf": tf,
            "bars": bars,
            "ema4": ema_closed(ctx.b4, ctx.ema4, ctx.end, t_from),
            "emad": ema_closed(ctx.bd, ctx.emad, ctx.end, t_from),
        }
    out["overlay"] = overlay(ctx, n_bars)
    return out


def trade_payload(ctx: m5.Context, t: dict[str, Any]) -> dict[str, Any]:
    """Chart of one trade with every rule element (trigger timeframe bars)."""
    step = int(t["trig_step"])
    b = ctx.b15 if step == m4.M15 else ctx.b30
    a = int(t["a_ts"]) - (30 if step == m4.M15 else 48) * m4.H1
    z = min(int(t.get("exit_ts") or ctx.end) + 6 * m4.H1, ctx.end)
    i0, i1 = np.searchsorted(b.ts, [a, z])
    bars = [
        [int(ts) // 1000, float(o), float(h), float(lo), float(c)]
        for ts, o, h, lo, c in zip(
            b.ts[i0:i1], b.o[i0:i1], b.h[i0:i1], b.l[i0:i1], b.c[i0:i1], strict=True
        )
    ]
    pois = []
    for p in str(t["pois"]).split(";"):
        kind, rest = p.split(":")
        near, far = (float(v) for v in rest.split("/"))
        pois.append({"kind": kind, "near": near, "far": far})
    return {
        "tf": "15m" if step == m4.M15 else "30m",
        "bars": bars,
        "ema4": ema_closed(ctx.b4, ctx.ema4, ctx.end, a),
        "emad": ema_closed(ctx.bd, ctx.emad, ctx.end, a),
        "pois": pois,
        "a_ts": int(t["a_ts"]) // 1000,
        "b_ts": int(t["b_ts"]) // 1000,
        "swing": _num(float(t["swing"])) if t.get("swing") is not None else None,
    }
