"""ITERATION-4 (FINAL): "TrejdingHub method replica", a sequential process.

Pre-registered in docs/research/ITERATION-4-PREREG.md (DECISIONS `ITERATION-4`); thresholds
in config `iteration4`. Candles = 1m klines aggregated to UTC-aligned 15m/30m/1h/4h bars; a bar
is known at its close. Orderflow (c2) = the existing 30m detectors on the aggTrade footprint.

1. bias on closed 4H bars (b1 EMA50 + slope, b2 2-bar-fractal structure);
2. POI: prior-day TPO VAL/POC (VAH/POC), prior-week VAL (VAH), fresh H1/M30 demand (supply)
   zones; first test only;
3. 15m liquidity sweep beyond the POI's far edge and the latest 15m swing, close back in the
   same or next bar;
4. confirmation c1 (engulfing / close in the top third) or c2 (c1 + 30m absorption or
   exhaustion overlapping the sweep, decision at that 30m close);
5. entry next 15m open; stop beyond the sweep extreme + buffer, >= max(k ATR, m RT cost);
   target = nearest structural level, only if >= minRR x stop; 24 h time stop; <= 1 trade
   per symbol per UTC day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import polars as pl

from flowlevels.config import AppConfig
from flowlevels.levels.profile import tpo_profile, value_area
from flowlevels.research.iteration1 import hourly_atr, round_trip_cost
from flowlevels.research.trades import Costs, Path1m, simulate

MIN = 60_000
M15, M30, H1, H4 = 15 * MIN, 30 * MIN, 60 * MIN, 240 * MIN
DAY = 86_400_000
WEEK = 7 * DAY
MONDAY_OFFSET_MS = 4 * DAY  # 1970-01-05 is a Monday
TF_MS = {"15m": M15, "30m": M30, "1h": H1, "4h": H4}


# --------------------------------------------------------------------------- bars


@dataclass(frozen=True)
class Bars:
    step: int
    ts: np.ndarray  # bar open (ms)
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray

    @property
    def close_ts(self) -> np.ndarray:
        return self.ts + self.step

    def last_closed(self, t: int) -> int:
        """Index of the last bar closed at or before t (-1 if none)."""
        return int(np.searchsorted(self.close_ts, t, side="right")) - 1


def make_bars(m: pl.DataFrame, step: int) -> Bars:
    g = (
        m.with_columns((pl.col("minute_ts") // step * step).alias("t"))
        .group_by("t", maintain_order=True)
        .agg(
            pl.col("open").first(), pl.col("high").max(), pl.col("low").min(),
            pl.col("close").last(),
        )
        .sort("t")
    )  # fmt: skip
    return Bars(step, *(g[c].to_numpy() for c in ("t", "open", "high", "low", "close")))


def fractals(h: np.ndarray, lo: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Strict n-bar fractals at bar j (needs n bars on each side); confirmed at j + n."""
    k = len(h)
    hi = np.zeros(k, bool)
    lw = np.zeros(k, bool)
    if k < 2 * n + 1:
        return hi, lw
    hi[n : k - n] = True
    lw[n : k - n] = True
    for d in range(1, n + 1):
        mid = slice(n, k - n)
        hi[mid] &= (h[mid] > h[n - d : k - n - d]) & (h[mid] > h[n + d : k - n + d])
        lw[mid] &= (lo[mid] < lo[n - d : k - n - d]) & (lo[mid] < lo[n + d : k - n + d])
    return hi, lw


def bias_b1(b: Bars, period: int, slope_bars: int) -> np.ndarray:
    ema = pl.Series(b.c).ewm_mean(span=period, adjust=False).to_numpy()
    slope = np.full(len(ema), np.nan)
    slope[slope_bars:] = ema[slope_bars:] - ema[:-slope_bars]
    out = np.zeros(len(ema), np.int8)
    out[(b.c > ema) & (slope > 0)] = 1
    out[(b.c < ema) & (slope < 0)] = -1
    return out


def bias_b2(b: Bars, n: int) -> np.ndarray:
    """Bias per bar k from the swings confirmed by its close (j + n <= k)."""
    is_hi, is_lo = fractals(b.h, b.l, n)
    out = np.zeros(len(b.ts), np.int8)
    highs: list[float] = []
    lows: list[float] = []
    for k in range(len(b.ts)):
        j = k - n
        if j >= 0:
            if is_hi[j]:
                highs.append(float(b.h[j]))
            if is_lo[j]:
                lows.append(float(b.l[j]))
        if len(highs) >= 2 and len(lows) >= 2:
            if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
                out[k] = 1
            elif highs[-1] < highs[-2] and lows[-1] < lows[-2]:
                out[k] = -1
    return out


def swing_before(b: Bars, n: int) -> tuple[np.ndarray, np.ndarray]:
    """(swing high, swing low) of the latest fractal confirmed at or before bar C's open,
    i.e. j + n <= C - 1, per bar C (nan if none)."""
    is_hi, is_lo = fractals(b.h, b.l, n)

    def ffill_shift(flag: np.ndarray, val: np.ndarray) -> np.ndarray:
        x = np.where(flag, val, np.nan)
        y = np.full(len(x), np.nan)
        if len(x) > n + 1:
            y[n + 1 :] = x[: len(x) - n - 1]
        idx = np.where(~np.isnan(y), np.arange(len(y)), -1)
        idx = np.maximum.accumulate(idx) if len(idx) else idx
        return np.where(idx >= 0, y[np.maximum(idx, 0)], np.nan)

    return ffill_shift(is_hi, b.h), ffill_shift(is_lo, b.l)


# --------------------------------------------------------------------------- POIs


@dataclass
class POI:
    direction: int  # +1 long (demand / support), -1 short
    kind: str  # day_val, day_poc, day_vah, week_val, week_vah, zone_1h, zone_30m
    near: float
    far: float
    start: int  # eligible from (ms)
    end: int  # eligible until (ms, exclusive)
    first_touch_ts: int = 2**62  # open of the first 15m bar touching the near edge


@dataclass
class VA:
    vah: float
    val: float
    poc: float


def va_of(df: pl.DataFrame, cfg: AppConfig, symbol: str) -> VA | None:
    sc, lc = cfg.symbol(symbol), cfg.levels
    va = value_area(
        tpo_profile(df, lc.tpo_period_minutes, sc.tpo_tick, sc.exchange_tick), lc.value_area_pct
    )
    return None if va is None else VA(va.vah, va.val, va.poc)


def week_start(t: int) -> int:
    return t - ((t - MONDAY_OFFSET_MS) % WEEK)


def zones(b: Bars, max_age_ms: int, tag: str) -> list[POI]:
    """Fresh demand (long) / supply (short) zones: base candle i-1 before an impulse i that
    leaves an FVG with i+1. Exists from the close of bar i+1."""
    out = []
    o, h, lo, c = b.o, b.h, b.l, b.c
    for i in range(1, len(b.ts) - 1):
        if b.ts[i + 1] - b.ts[i - 1] != 2 * b.step:
            continue  # missing bar: no 3 consecutive bars
        t0 = int(b.ts[i + 1] + b.step)
        if c[i] > o[i] and lo[i + 1] > h[i - 1]:
            top, bot = max(o[i - 1], c[i - 1]), lo[i - 1]
            if top > bot:
                out.append(POI(1, tag, float(top), float(bot), t0, t0 + max_age_ms))
        if c[i] < o[i] and h[i + 1] < lo[i - 1]:
            bot, top = min(o[i - 1], c[i - 1]), h[i - 1]
            if top > bot:
                out.append(POI(-1, tag, float(bot), float(top), t0, t0 + max_age_ms))
    return out


@dataclass
class Levels:
    day: dict[int, VA] = field(default_factory=dict)
    week: dict[int, VA] = field(default_factory=dict)
    day_open: dict[int, float] = field(default_factory=dict)


def static_levels(m: pl.DataFrame, days: list[int], cfg: AppConfig, symbol: str) -> Levels:
    ts = m["minute_ts"].to_numpy()
    lv = Levels()
    for d0 in days:
        a, b = np.searchsorted(ts, [d0 - DAY, d0])
        if b - a >= 1380 and b < len(ts) and int(ts[b]) == d0:
            va = va_of(m[int(a) : int(b)], cfg, symbol)
            if va is not None:
                lv.day[d0] = va
                lv.day_open[d0] = float(m["open"][int(b)])
        w0 = week_start(d0)
        if w0 not in lv.week:
            a, b = np.searchsorted(ts, [w0 - WEEK, w0])
            if b - a >= 7 * 1380:
                va = va_of(m[int(a) : int(b)], cfg, symbol)
                if va is not None:
                    lv.week[w0] = va
    return lv


def level_pois(lv: Levels, b15: Bars, days: list[int]) -> list[POI]:
    out: list[POI] = []
    for d0 in days:
        if d0 not in lv.day:
            continue
        va, op = lv.day[d0], lv.day_open[d0]
        for kind, x, d in (("day_val", va.val, 1), ("day_poc", va.poc, 1),
                           ("day_vah", va.vah, -1), ("day_poc", va.poc, -1)):  # fmt: skip
            if d * (op - x) > 0:
                out.append(POI(d, kind, x, x, d0, d0 + DAY))
        w0 = week_start(d0)
        if w0 not in lv.week:
            continue
        wv = lv.week[w0]
        i0, i1 = np.searchsorted(b15.ts, [w0, d0])
        for kind, x, d in (("week_val", wv.val, 1), ("week_vah", wv.vah, -1)):
            if d * (op - x) <= 0:
                continue
            touched = bool(((b15.l[i0:i1] <= x) & (b15.h[i0:i1] >= x)).any())
            if not touched:
                out.append(POI(d, kind, x, x, d0, d0 + DAY))
    return out


# --------------------------------------------------------------------------- sweeps


@dataclass
class Setup:
    direction: int
    c_idx: int  # sweep candidate (15m index)
    k_idx: int  # close-back bar
    extreme: float
    swing: float
    pois: list[POI]


def scan_poi(
    p: POI, b: Bars, swing_hi: np.ndarray, swing_lo: np.ndarray, tick: float
) -> Setup | None:
    """First-test logic (pre-registration step 2/3). Sets p.first_touch_ts. Setups before
    a window are produced too and dropped by the runner (decision outside the window)."""
    i0 = int(np.searchsorted(b.ts, p.start))
    i1 = int(np.searchsorted(b.ts, p.end))
    if i0 >= i1:
        return None
    d = p.direction
    touch = b.l[i0:i1] <= p.near if d > 0 else b.h[i0:i1] >= p.near
    if not touch.any():
        return None
    t0 = i0 + int(np.argmax(touch))
    p.first_touch_ts = int(b.ts[t0])
    if d > 0:
        sw = b.l[t0:i1] <= p.far - tick
        back = b.c[t0:i1] > p.near
    else:
        sw = b.h[t0:i1] >= p.far + tick
        back = b.c[t0:i1] < p.near
    if not sw.any():
        return None
    s = int(np.argmax(sw))
    if back.any() and int(np.argmax(back)) < s:
        return None  # consumed: closed back beyond the near edge before any sweep
    ci = t0 + s
    swing = swing_lo[ci] if d > 0 else swing_hi[ci]
    if np.isnan(swing):
        return None
    if d > 0 and not b.l[ci] <= swing - tick:
        return None
    if d < 0 and not b.h[ci] >= swing + tick:
        return None
    if d * (b.c[ci] - p.far) > 0:
        ki = ci
    elif ci + 1 < len(b.ts) and d * (b.c[ci + 1] - p.far) > 0:
        ki = ci + 1
    else:
        return None
    ext = float(b.l[ci : ki + 1].min()) if d > 0 else float(b.h[ci : ki + 1].max())
    return Setup(d, ci, ki, ext, float(swing), [p])


def merge_setups(ss: list[Setup]) -> list[Setup]:
    """One setup per (direction, close-back bar): earliest C, most extreme sweep."""
    by: dict[tuple[int, int], Setup] = {}
    for s in ss:
        key = (s.direction, s.k_idx)
        if key not in by:
            by[key] = Setup(s.direction, s.c_idx, s.k_idx, s.extreme, s.swing, list(s.pois))
            continue
        m = by[key]
        m.c_idx = min(m.c_idx, s.c_idx)
        m.extreme = min(m.extreme, s.extreme) if s.direction > 0 else max(m.extreme, s.extreme)
        m.swing = min(m.swing, s.swing) if s.direction > 0 else max(m.swing, s.swing)
        m.pois += s.pois
    return sorted(by.values(), key=lambda s: (s.k_idx, s.direction))


def c1_ok(b: Bars, k: int, d: int, top_third: float) -> bool:
    o, h, lo, c = b.o, b.h, b.l, b.c
    rng = h[k] - lo[k]
    if d > 0:
        third = rng > 0 and (c[k] - lo[k]) / rng >= top_third
        engulf = (
            k > 0 and c[k] > o[k] and c[k - 1] < o[k - 1] and c[k] >= o[k - 1] and o[k] <= c[k - 1]
        )
    else:
        third = rng > 0 and (h[k] - c[k]) / rng >= top_third
        engulf = (
            k > 0 and c[k] < o[k] and c[k - 1] > o[k - 1] and c[k] <= o[k - 1] and o[k] >= c[k - 1]
        )
    return bool(third or engulf)


@dataclass
class OrderflowFlags:
    """30m detector flags: bar_ts -> (long_ok, short_ok, usable)."""

    ts: np.ndarray
    long: np.ndarray
    short: np.ndarray
    usable: np.ndarray

    def confirm(self, t_from: int, t_to: int, d: int) -> tuple[bool, bool]:
        """(confirmed, all_bars_usable) over the 30m bars with open in [t_from, t_to]."""
        need = np.arange(t_from // M30 * M30, t_to // M30 * M30 + 1, M30)
        idx = np.searchsorted(self.ts, need)
        ok = (idx < len(self.ts)) & (self.ts[np.minimum(idx, len(self.ts) - 1)] == need)
        if not ok.all():
            return False, False
        if not self.usable[idx].all():
            return False, False
        f = self.long if d > 0 else self.short
        return bool(f[idx].any()), True


def orderflow_flags(feats: pl.DataFrame, detectors: list[str]) -> OrderflowFlags:
    lg = pl.lit(False)
    sh = pl.lit(False)
    for d in detectors:
        lg = lg | pl.col(f"{d}_long")
        sh = sh | pl.col(f"{d}_short")
    x = feats.select("bar_ts", lg.alias("l"), sh.alias("s"), (~pl.col("in_hole")).alias("u"))
    return OrderflowFlags(*(x[c].to_numpy() for c in ("bar_ts", "l", "s", "u")))


# --------------------------------------------------------------------------- runner


@dataclass(frozen=True)
class Grid:
    bias: str
    conf: str
    min_rr: float

    @property
    def key(self) -> str:
        return f"{self.bias}|{self.conf}|rr{self.min_rr:g}"


def grids(cfg: AppConfig) -> list[Grid]:
    i4 = cfg.iteration4
    return [Grid(b, c, r) for b in i4.bias for c in i4.confirmation for r in i4.min_rr]


def _target(d: int, entry: float, entry_ts: int, lv: Levels, zs: list[POI]) -> float | None:
    d0 = entry_ts // DAY * DAY
    cand: list[float] = []
    if d0 in lv.day:
        va = lv.day[d0]
        cand += [va.vah, va.val, va.poc]
    w0 = week_start(d0)
    if w0 in lv.week:
        wv = lv.week[w0]
        cand += [wv.vah, wv.val, wv.poc]
    for z in zs:  # opposite zones, unexpired and untested at the entry
        if z.direction == -d and z.start <= entry_ts < z.end and z.first_touch_ts >= entry_ts:
            cand.append(z.near)
    ahead = [x for x in cand if d * (x - entry) > 0]
    if not ahead:
        return None
    return min(ahead) if d > 0 else max(ahead)


def run_symbol(
    m: pl.DataFrame,
    of: OrderflowFlags | None,
    start: int,
    end: int,
    cfg: AppConfig,
    symbol: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """All grid trades for one symbol in [start, end). m = 1m klines incl. warm-up."""
    i4 = cfg.iteration4
    tick = cfg.symbol(symbol).exchange_tick
    buf = cfg.trade_model.stop_buffer_ticks * cfg.costs.execution_instruments[symbol].tick
    b15, b4 = make_bars(m, M15), make_bars(m, H4)
    bias = {"b1": bias_b1(b4, i4.ema_period, i4.ema_slope_bars), "b2": bias_b2(b4, i4.fractal_n)}
    sw_hi, sw_lo = swing_before(b15, i4.fractal_n)
    days = list(range(start, end, DAY))
    lv = static_levels(m, days, cfg, symbol)
    pois = level_pois(lv, b15, days)
    zs: list[POI] = []
    for tf in i4.zone_timeframes:
        zs += zones(make_bars(m, TF_MS[tf]), i4.zone_max_age_h * H1, f"zone_{tf}")
    raw: list[Setup] = []
    for p in pois + zs:
        s = scan_poi(p, b15, sw_hi, sw_lo, tick)
        if s is not None:
            raw.append(s)
    setups = merge_setups(raw)
    path = Path1m(*(m[c].to_numpy() for c in ("minute_ts", "open", "high", "low", "close")))
    atr_ts, atr = hourly_atr(m, cfg.levels.atr_period_1h)

    meta: dict[str, Any] = {
        "pois": {}, "setups": len(setups), "c1": 0, "c2": 0, "c2_no_footprint": 0, "skips": {},
        "c2_keys": [],
    }  # fmt: skip
    for p in pois + zs:
        if p.start < end and p.end > start:
            meta["pois"][p.kind] = meta["pois"].get(p.kind, 0) + 1
    # confirmation per setup -> decision times
    cands: dict[str, list[tuple[int, Setup]]] = {"c1": [], "c2": []}
    for s in setups:
        if not c1_ok(b15, s.k_idx, s.direction, i4.top_third):
            continue
        k_close = int(b15.ts[s.k_idx] + M15)
        cands["c1"].append((k_close, s))
        meta["c1"] += 1
        if of is None:
            meta["c2_no_footprint"] += 1
            continue
        ok, usable = of.confirm(int(b15.ts[s.c_idx]), int(b15.ts[s.k_idx]), s.direction)
        if not usable:
            meta["c2_no_footprint"] += 1
        if ok:
            meta["c2"] += 1
            meta["c2_keys"].append((symbol, int(b15.ts[s.k_idx]), s.direction))
            cands["c2"].append((int(b15.ts[s.k_idx]) // M30 * M30 + M30, s))

    rows: list[dict[str, Any]] = []
    for g in grids(cfg):
        skips: dict[str, int] = {}

        def skip(r: str, _s: dict[str, int] = skips) -> None:
            _s[r] = _s.get(r, 0) + 1

        busy_until = -1
        taken_days: set[int] = set()
        for dec, s in sorted(cands[g.conf], key=lambda x: (x[0], x[1].direction)):
            if not (start <= dec < end):
                skip("outside_window")
                continue
            k4 = b4.last_closed(dec)
            if k4 < 0 or bias[g.bias][k4] != s.direction:
                skip("bias")
                continue
            entry_ts = dec
            if entry_ts // DAY in taken_days:
                skip("daily_limit")
                continue
            if entry_ts <= busy_until:
                skip("position_open")
                continue
            i = path.index(entry_ts)
            j = int(np.searchsorted(atr_ts, entry_ts, side="right")) - 1
            if i is None or j < 0:
                skip("no_data")
                continue
            entry = float(path.open[i])
            d = s.direction
            target = _target(d, entry, entry_ts, lv, zs)
            if target is None:
                skip("no_target")
                continue
            mind = max(
                i4.min_stop_k_atr * float(atr[j]),
                i4.min_stop_cost_mult * round_trip_cost(cfg, symbol, entry),
            )
            stop = s.extreme - d * buf
            hold = i4.time_stop_minutes
            f = simulate(path, entry_ts, d, stop, mind, "zone", target, hold, Costs(0, 0, 0))
            if not f.valid:
                skip(f.reason)
                continue
            tdist = d * (target - f.entry)
            if tdist < g.min_rr * f.r_unit:
                skip("rr_too_low")
                continue
            busy_until = f.exit_ts
            taken_days.add(entry_ts // DAY)
            rows.append(
                {
                    "symbol": symbol, "config": g.key, "bias": g.bias, "conf": g.conf,
                    "min_rr": g.min_rr, "direction": d, "decision_ts": dec, "entry_ts": entry_ts,
                    "session": entry_ts // DAY * DAY, "entry": f.entry, "stop": f.stop,
                    "r_unit": f.r_unit, "target": target, "target_r": tdist / f.r_unit,
                    "exit_ts": f.exit_ts, "exit_px": f.exit_px, "exit_kind": f.exit_kind,
                    "gross_r": f.gross_r, "mfe_r": f.mfe_r, "mae_r": f.mae_r, "valid": True,
                    "sweep_ts": int(b15.ts[s.c_idx]), "closeback_ts": int(b15.ts[s.k_idx]),
                    "extreme": s.extreme, "swing": s.swing,
                    "pois": ";".join(f"{p.kind}:{p.near:g}/{p.far:g}" for p in s.pois),
                    "window_end_close": bool(
                        f.exit_kind == "time" and entry_ts + hold * MIN > end
                    ),
                }
            )  # fmt: skip
        meta["skips"][g.key] = skips
    return rows, meta
