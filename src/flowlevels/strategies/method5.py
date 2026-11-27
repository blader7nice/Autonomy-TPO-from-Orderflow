"""ITERATION-5 (FINAL, binding hold-out): TrejdingHub method, revised.

Pre-registered in docs/research/ITERATION-5-PREREG.md (DECISIONS `ITERATION-5`); thresholds in
config `iteration5`. Candles only. Reuses the ITERATION-4 bars, POIs, sweep scan and c1 candle
(`strategies.method4`). Changes vs ITERATION-4:

1. bias at entry = 4H b1 AND entry price on the bias side of that 4H EMA50 AND the last daily
   close on the same side of the daily EMA50;
2. triggers T1 (sweep + close-back + c1, sweep range >= 1.5 x ATR(15m)) and T2 (30m touch of the
   POI, then an engulfing or pin-bar 30m candle within 3 bars that does not close beyond the far
   edge);
3. stop = POI far edge + k x ATR(1h), >= max(0.5 x ATR(1h), 3 x round-trip cost);
4. 50 % at TP1 (nearest structural level, >= 1 R) + 50 % at TP2 (next level) or by a
   1.5 x ATR(1h) trail after TP1; 48 h time stop; <= 1 trade per symbol per UTC day.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import polars as pl

from flowlevels.config import AppConfig
from flowlevels.research.iteration1 import hourly_atr, round_trip_cost
from flowlevels.research.trades import Path1m, plan_stop
from flowlevels.strategies import method4 as m4
from flowlevels.strategies.method4 import DAY, H1, H4, M15, M30, MIN, POI, Bars

# --------------------------------------------------------------------------- helpers


def ema(x: np.ndarray, period: int) -> np.ndarray:
    return pl.Series(x).ewm_mean(span=period, adjust=False).to_numpy()


def atr_before(b: Bars, period: int) -> np.ndarray:
    """ATR per bar C = SMA(period) of the true range of bars C-period .. C-1 (nan if short).
    The true range of bar 0 is undefined (no previous close)."""
    pc = np.r_[np.nan, b.c[:-1]]
    tr = np.maximum(b.h - b.l, np.maximum(np.abs(b.h - pc), np.abs(b.l - pc)))
    tr[0] = 0.0
    cs = np.r_[0.0, np.cumsum(tr)]
    out = np.full(len(tr), np.nan)
    c = np.arange(period + 1, len(tr))
    out[c] = (cs[c] - cs[c - period]) / period
    return out


def first_touch(p: POI, b15: Bars) -> None:
    """Open of the first 15m bar reaching the near edge (for 'untested at entry' targets)."""
    i0 = int(np.searchsorted(b15.ts, p.start))
    i1 = int(np.searchsorted(b15.ts, p.end))
    if i0 >= i1:
        return
    t = b15.l[i0:i1] <= p.near if p.direction > 0 else b15.h[i0:i1] >= p.near
    if t.any():
        p.first_touch_ts = int(b15.ts[i0 + int(np.argmax(t))])


@dataclass
class Trig:
    """A triggered setup: decision (= entry) time and the POIs behind it."""

    trigger: str  # T1 | T2
    direction: int
    dec_ts: int
    a_ts: int  # T1: sweep bar C (15m open); T2: touch bar t0 (30m open)
    b_ts: int  # T1: close-back bar K; T2: confirmation bar j
    step: int  # 15m or 30m
    extreme: float  # T1: sweep extreme; T2: extreme of t0 .. j
    swing: float  # T1 only (nan for T2)
    pois: list[POI]
    pattern: str = ""  # T1: 'sweep'; T2: 'engulf' | 'pin' (confirmation candle)

    @property
    def far(self) -> float:
        """Farthest far edge among the POIs (pre-registration ambiguity 2)."""
        fs = [p.far for p in self.pois]
        return min(fs) if self.direction > 0 else max(fs)


def _merge(ts: list[Trig]) -> list[Trig]:
    by: dict[tuple[str, int, int], Trig] = {}
    for t in ts:
        key = (t.trigger, t.direction, t.dec_ts)
        if key not in by:
            by[key] = Trig(**{**t.__dict__, "pois": list(t.pois)})
            continue
        m = by[key]
        m.a_ts = min(m.a_ts, t.a_ts)
        m.extreme = min(m.extreme, t.extreme) if t.direction > 0 else max(m.extreme, t.extreme)
        if not np.isnan(t.swing):
            m.swing = (
                t.swing
                if np.isnan(m.swing)
                else (min(m.swing, t.swing) if t.direction > 0 else max(m.swing, t.swing))
            )
        m.pois += t.pois
    return sorted(by.values(), key=lambda t: (t.dec_ts, t.direction))


# --------------------------------------------------------------------------- triggers


def t1_setups(
    pois: list[POI], b15: Bars, cfg: AppConfig, tick: float, meta: dict[str, Any]
) -> list[Trig]:
    i5 = cfg.iteration5
    sw_hi, sw_lo = m4.swing_before(b15, i5.fractal_n)
    atr15 = atr_before(b15, i5.atr15_period)
    raw = []
    for p in pois:
        s = m4.scan_poi(p, b15, sw_hi, sw_lo, tick * i5.sweep_ticks)
        if s is not None:
            raw.append(s)
    merged = m4.merge_setups(raw)
    meta["t1_sweeps"] = len(merged)
    out = []
    for s in merged:
        if not m4.c1_ok(b15, s.k_idx, s.direction, i5.top_third):
            meta["t1_no_c1"] = meta.get("t1_no_c1", 0) + 1
            continue
        a = atr15[s.c_idx]
        if not (np.isfinite(a) and b15.h[s.c_idx] - b15.l[s.c_idx] >= i5.sweep_range_k_atr15 * a):
            meta["t1_small_range"] = meta.get("t1_small_range", 0) + 1
            continue
        out.append(
            Trig(
                "T1", s.direction, int(b15.ts[s.k_idx] + M15), int(b15.ts[s.c_idx]),
                int(b15.ts[s.k_idx]), M15, s.extreme, s.swing, list(s.pois), "sweep",
            )
        )  # fmt: skip
    meta["t1"] = len(out)
    return out


def confirm_t2(b: Bars, j: int, d: int, far: float, wick_body: float, third: float) -> str:
    """'' if bar j is not a T2 confirmation candle, else 'engulf' or 'pin'."""
    o, h, lo, c = b.o[j], b.h[j], b.l[j], b.c[j]
    if d * (c - far) < 0:
        return ""  # closes beyond the far edge
    body = abs(c - o)
    if j > 0:
        po, pc = b.o[j - 1], b.c[j - 1]
        pbody = abs(pc - po)
        if d > 0 and c > o and o <= min(po, pc) and c >= max(po, pc) and body > pbody:
            return "engulf"
        if d < 0 and c < o and o >= max(po, pc) and c <= min(po, pc) and body > pbody:
            return "engulf"
    rng = h - lo
    if rng > 0:
        if d > 0 and min(o, c) - lo >= wick_body * body and (c - lo) / rng >= third:
            return "pin"
        if d < 0 and h - max(o, c) >= wick_body * body and (h - c) / rng >= third:
            return "pin"
    return ""


def t2_setups(pois: list[POI], b30: Bars, cfg: AppConfig, meta: dict[str, Any]) -> list[Trig]:
    i5 = cfg.iteration5
    raw = []
    kinds: dict[str, int] = {}
    for p in pois:
        i0 = int(np.searchsorted(b30.ts, p.start))
        i1 = int(np.searchsorted(b30.ts, p.end))
        if i0 >= i1:
            continue
        d = p.direction
        touch = b30.l[i0:i1] <= p.near if d > 0 else b30.h[i0:i1] >= p.near
        if not touch.any():
            continue
        t0 = i0 + int(np.argmax(touch))
        meta["t2_touches"] = meta.get("t2_touches", 0) + 1
        for j in range(t0, t0 + i5.t2_confirm_bars):
            if j >= len(b30.ts) or b30.ts[j] != b30.ts[t0] + (j - t0) * M30:
                break  # consecutive bars only
            kind = confirm_t2(b30, j, d, p.far, i5.pin_wick_body, i5.pin_close_third)
            if kind:
                kinds[kind] = kinds.get(kind, 0) + 1
                ext = float(b30.l[t0 : j + 1].min()) if d > 0 else float(b30.h[t0 : j + 1].max())
                raw.append(
                    Trig("T2", d, int(b30.ts[j] + M30), int(b30.ts[t0]), int(b30.ts[j]), M30,
                         ext, float("nan"), [p], kind)
                )  # fmt: skip
                break
    out = _merge(raw)
    meta["t2_kinds"] = kinds
    meta["t2"] = len(out)
    return out


# --------------------------------------------------------------------------- exits


def levels_ahead(d: int, entry: float, entry_ts: int, lv: m4.Levels, zs: list[POI]) -> list[float]:
    """Structural levels strictly beyond the entry, nearest first (ITERATION-4 set)."""
    d0 = entry_ts // DAY * DAY
    cand: list[float] = []
    if d0 in lv.day:
        va = lv.day[d0]
        cand += [va.vah, va.val, va.poc]
    w0 = m4.week_start(d0)
    if w0 in lv.week:
        wv = lv.week[w0]
        cand += [wv.vah, wv.val, wv.poc]
    for z in zs:
        if z.direction == -d and z.start <= entry_ts < z.end and z.first_touch_ts >= entry_ts:
            cand.append(z.near)
    ahead = sorted({x for x in cand if d * (x - entry) > 0}, key=lambda x: d * (x - entry))
    return ahead


@dataclass
class Split:
    valid: bool
    reason: str = ""
    entry: float = float("nan")
    stop: float = float("nan")
    r_unit: float = float("nan")
    x1_ts: int = 0
    x1_px: float = float("nan")
    x2_ts: int = 0
    x2_px: float = float("nan")
    kind: str = ""
    mfe_r: float = float("nan")
    mae_r: float = float("nan")


def simulate_split(
    path: Path1m,
    i0: int,
    d: int,
    stop: float,
    r: float,
    tp1: float,
    tp2: float | None,
    mode: str,
    trail_dist: float,
    hold_min: int,
) -> Split:
    """50/50 exit on the 1m path from minute index i0 (entry at its open).

    Stop before target within a minute. `lvl`: second half at tp2 (or stop / time). `trail`:
    after TP1, stop = max(stop, best - trail_dist) from completed minutes (long; mirrored),
    filled at the trail or at the minute open if it opened beyond it."""
    entry = float(path.open[i0])
    end_ts = int(path.ts[i0]) + hold_min * MIN
    i1 = int(np.searchsorted(path.ts, end_ts))
    hi, lo, op, cl = path.high[i0:i1], path.low[i0:i1], path.open[i0:i1], path.close[i0:i1]
    n = len(hi)
    adv = lo if d > 0 else hi  # adverse extreme per minute
    fav = hi if d > 0 else lo

    def hit_stop(lvl: float, x: np.ndarray) -> np.ndarray:
        return x <= lvl if d > 0 else x >= lvl

    def hit_tgt(lvl: float, x: np.ndarray) -> np.ndarray:
        return x >= lvl if d > 0 else x <= lvl

    def first(mask: np.ndarray, frm: int = 0) -> int:
        m = mask[frm:]
        return frm + int(np.argmax(m)) if m.any() else n

    def ts(k: int) -> int:
        return int(path.ts[i0 + k])

    def excursions(k_end: int, stopped: bool) -> tuple[float, float]:
        mfe = (
            float((fav[: k_end + 1] - entry).max())
            if d > 0
            else float((entry - fav[: k_end + 1]).max())
        )
        mae = (
            float((entry - adv[: k_end + 1]).max())
            if d > 0
            else float((adv[: k_end + 1] - entry).max())
        )
        return mfe / r, (min(mae, r) if stopped else mae) / r

    ks = first(hit_stop(stop, adv))
    kt = first(hit_tgt(tp1, fav))
    if ks <= kt and ks < n:
        mf, ma = excursions(ks, True)
        return Split(True, "", entry, stop, r, ts(ks), stop, ts(ks), stop, "stop", mf, ma)
    if kt >= n:
        mf, ma = excursions(n - 1, False)
        px = float(cl[n - 1])
        return Split(True, "", entry, stop, r, ts(n - 1), px, ts(n - 1), px, "time", mf, ma)
    x1_ts, x1 = ts(kt), float(tp1)
    if mode == "lvl":
        k2s = first(hit_stop(stop, adv), kt)
        k2t = first(hit_tgt(tp2, fav), kt) if tp2 is not None else n
        if k2s <= k2t and k2s < n:
            k, px, kind = k2s, stop, "tp1+stop"
        elif k2t < n:
            k, px, kind = k2t, float(tp2), "tp1+tp2"  # type: ignore[arg-type]
        else:
            k, px, kind = n - 1, float(cl[n - 1]), "tp1+time"
    else:
        k, px, kind = n - 1, float(cl[n - 1]), "tp1+time"
        if kt + 1 < n:
            best = (
                np.maximum.accumulate(fav[kt : n - 1])
                if d > 0
                else np.minimum.accumulate(fav[kt : n - 1])
            )
            trail = (
                np.maximum(stop, best - trail_dist)
                if d > 0
                else np.minimum(stop, best + trail_dist)
            )  # trail for minutes kt+1 .. n-1
            hit = adv[kt + 1 : n] <= trail if d > 0 else adv[kt + 1 : n] >= trail
            if hit.any():
                q = int(np.argmax(hit))
                k = kt + 1 + q
                o = float(op[k])
                px = min(o, float(trail[q])) if d > 0 else max(o, float(trail[q]))
                kind = "tp1+trail"
    mf, ma = excursions(k, False)
    return Split(True, "", entry, stop, r, x1_ts, x1, ts(k), px, kind, mf, ma)


# --------------------------------------------------------------------------- runner


@dataclass(frozen=True)
class Grid:
    """One configuration. stop 'far': POI far edge - d x k x ATR(1h) (ITERATION-5 grid).
    stop 'wick' (paper variant c, T2 only): the 30m confirmation candle's wick extreme
    - d x k x ATR(1h)."""

    trigger: str
    k: float
    exit2: str
    stop: str = "far"

    @property
    def key(self) -> str:
        if self.stop == "wick":
            return f"{self.trigger}|wick|{self.exit2}"
        return f"{self.trigger}|k{self.k:g}|{self.exit2}"


def grids(cfg: AppConfig) -> list[Grid]:
    i5 = cfg.iteration5
    return [Grid(t, k, e) for t in i5.trigger for k in i5.stop_k_atr for e in i5.second_exit]


@dataclass
class Context:
    """Everything the trade rules need for one symbol, computed once from the 1m klines `m`
    (warm-up included, ending at `end`): bars, bias inputs, levels, POIs and triggers."""

    symbol: str
    start: int
    end: int
    m: pl.DataFrame
    b15: Bars
    b30: Bars
    b4: Bars
    bd: Bars
    bias4: np.ndarray
    ema4: np.ndarray
    emad: np.ndarray
    lv: m4.Levels
    pois: list[POI]
    zs: list[POI]
    trig: dict[str, list[Trig]]
    path: Path1m
    atr_ts: np.ndarray
    atr: np.ndarray
    meta: dict[str, Any]


def prepare(m: pl.DataFrame, start: int, end: int, cfg: AppConfig, symbol: str) -> Context:
    i5 = cfg.iteration5
    tick = cfg.symbol(symbol).exchange_tick
    b15, b30, b4, bd = (m4.make_bars(m, s) for s in (M15, M30, H4, DAY))
    bias4 = m4.bias_b1(b4, i5.ema_period, i5.ema_slope_bars)
    ema4 = ema(b4.c, i5.ema_period)
    emad = ema(bd.c, i5.ema_period)
    days = list(range(start, end, DAY))
    lv = m4.static_levels(m, days, cfg, symbol)
    pois = m4.level_pois(lv, b15, days)
    zs: list[POI] = []
    for tf in i5.zone_timeframes:
        zs += m4.zones(m4.make_bars(m, m4.TF_MS[tf]), i5.zone_max_age_h * H1, f"zone_{tf}")
    for p in pois + zs:
        first_touch(p, b15)
    meta: dict[str, Any] = {"pois": {}, "skips": {}}
    for p in pois + zs:
        if p.start < end and p.end > start:
            meta["pois"][p.kind] = meta["pois"].get(p.kind, 0) + 1
    # scan_poi (T1) sets first_touch_ts to the same 15m first touch as first_touch() above.
    trig = {
        "T1": t1_setups(pois + zs, b15, cfg, tick, meta),
        "T2": t2_setups(pois + zs, b30, cfg, meta),
    }
    path = Path1m(*(m[c].to_numpy() for c in ("minute_ts", "open", "high", "low", "close")))
    atr_ts, atr = hourly_atr(m, cfg.levels.atr_period_1h)
    return Context(symbol, start, end, m, b15, b30, b4, bd, bias4, ema4, emad, lv, pois, zs,
                   trig, path, atr_ts, atr, meta)  # fmt: skip


def stop_anchor(ctx: Context, t: Trig, g: Grid) -> float:
    """Price the k x ATR buffer is measured from: the far edge, or (wick) the confirmation
    candle's wick extreme (bar j = t.b_ts on 30m)."""
    if g.stop == "far":
        return t.far
    if g.stop == "wick":
        if t.trigger != "T2":
            raise ValueError("the wick stop is defined for T2 only")
        j = int(np.searchsorted(ctx.b30.ts, t.b_ts))
        return float(ctx.b30.l[j]) if t.direction > 0 else float(ctx.b30.h[j])
    raise ValueError(f"unknown stop mode {g.stop!r}")


def trades(
    ctx: Context,
    cfg: AppConfig,
    gl: list[Grid],
    signals: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Trades of every grid in `gl` with entries in [ctx.start, ctx.end). If `signals` is a
    list, one row per setup inside the window is appended (taken or the skip reason)."""
    i5 = cfg.iteration5
    symbol, start, end = ctx.symbol, ctx.start, ctx.end
    path, b4, bd = ctx.path, ctx.b4, ctx.bd
    rows: list[dict[str, Any]] = []
    for g in gl:
        skips: dict[str, int] = {}

        def skip(r: str, t: Trig, _s: dict[str, int] = skips, _g: Grid = g) -> None:
            _s[r] = _s.get(r, 0) + 1
            if signals is not None and r != "outside_window":
                signals.append(_signal(symbol, _g, t, r))

        busy_until = -1
        taken_days: set[int] = set()
        for t in ctx.trig[g.trigger]:
            et, d = t.dec_ts, t.direction
            if not (start <= et < end):
                skip("outside_window", t)
                continue
            i = path.index(et)
            j = int(np.searchsorted(ctx.atr_ts, et, side="right")) - 1
            k4, kd = b4.last_closed(et), bd.last_closed(et)
            if i is None or j < 0 or k4 < 0 or kd < 0:
                skip("no_data", t)
                continue
            entry = float(path.open[i])
            if not (
                ctx.bias4[k4] == d
                and d * (entry - ctx.ema4[k4]) > 0
                and d * (bd.c[kd] - ctx.emad[kd]) > 0
            ):
                skip("bias", t)
                continue
            if et // DAY in taken_days:
                skip("daily_limit", t)
                continue
            if et <= busy_until:
                skip("position_open", t)
                continue
            a1h = float(ctx.atr[j])
            mind = max(
                i5.min_stop_k_atr * a1h, i5.min_stop_cost_mult * round_trip_cost(cfg, symbol, entry)
            )
            anchor = stop_anchor(ctx, t, g)
            stop, r = plan_stop(entry, anchor - d * g.k * a1h, d, mind)
            if r <= 0:
                skip("stop_beyond_entry", t)
                continue
            ahead = levels_ahead(d, entry, et, ctx.lv, ctx.zs)
            if not ahead:
                skip("no_target", t)
                continue
            tp1 = ahead[0]
            if d * (tp1 - entry) < i5.tp1_min_r * r:
                skip("tp1_below_1r", t)
                continue
            tp2 = ahead[1] if len(ahead) > 1 else None
            f = simulate_split(
                path, i, d, stop, r, tp1, tp2, g.exit2, i5.trail_k_atr * a1h, i5.time_stop_minutes
            )
            fr = i5.tp1_fraction
            exit_px = fr * f.x1_px + (1 - fr) * f.x2_px
            exit_ts = max(f.x1_ts, f.x2_ts)
            busy_until = exit_ts
            taken_days.add(et // DAY)
            if signals is not None:
                signals.append(_signal(symbol, g, t, "taken"))
            rows.append(
                {
                    "symbol": symbol, "config": g.key, "trigger": g.trigger, "k": g.k,
                    "exit2": g.exit2, "direction": d, "decision_ts": et, "entry_ts": et,
                    "session": et // DAY * DAY, "entry": f.entry, "stop": f.stop,
                    "r_unit": f.r_unit, "far": t.far, "atr1h": a1h, "tp1": tp1,
                    "tp2": float("nan") if tp2 is None else tp2,
                    "tp1_r": d * (tp1 - entry) / r,
                    "x1_ts": f.x1_ts, "x1_px": f.x1_px, "x2_ts": f.x2_ts, "x2_px": f.x2_px,
                    "exit_ts": exit_ts, "exit_px": exit_px, "exit_kind": f.kind,
                    "gross_r": d * (exit_px - f.entry) / r, "mfe_r": f.mfe_r, "mae_r": f.mae_r,
                    "valid": True, "a_ts": t.a_ts, "b_ts": t.b_ts, "trig_step": t.step,
                    "extreme": t.extreme, "swing": t.swing, "pattern": t.pattern,
                    "ema4": float(ctx.ema4[k4]), "emad": float(ctx.emad[kd]), "close_d": float(bd.c[kd]),
                    "pois": ";".join(f"{p.kind}:{p.near:g}/{p.far:g}" for p in t.pois),
                    "window_end_close": bool(
                        f.kind.endswith("time") and et + i5.time_stop_minutes * MIN > end
                    ),
                }
            )  # fmt: skip
            if g.stop != "far":
                rows[-1]["stop_anchor"] = anchor
        ctx.meta["skips"][g.key] = skips
    return rows


def _signal(symbol: str, g: Grid, t: Trig, outcome: str) -> dict[str, Any]:
    return {
        "symbol": symbol, "config": g.key, "trigger": t.trigger, "direction": t.direction,
        "decision_ts": t.dec_ts, "a_ts": t.a_ts, "b_ts": t.b_ts, "far": t.far,
        "pois": ";".join(f"{p.kind}:{p.near:g}/{p.far:g}" for p in t.pois), "outcome": outcome,
    }  # fmt: skip


def run_symbol(
    m: pl.DataFrame,
    start: int,
    end: int,
    cfg: AppConfig,
    symbol: str,
    gl: list[Grid] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """All grid trades for one symbol with entries in [start, end). m = 1m klines incl.
    warm-up, ending at `end` (nothing after the window is loaded)."""
    ctx = prepare(m, start, end, cfg, symbol)
    rows = trades(ctx, cfg, grids(cfg) if gl is None else gl)
    return rows, ctx.meta
