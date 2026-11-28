"""Port of the owner's live Supply Zone bot (docs/strategies/SUPPLY_ZONE_SPEC.md).

The live code is pandas-based; this port reproduces its numerics with numpy:
`ewm(span, adjust=False)` recursion, `rolling(n).mean()` (NaN until n values), the true
range with the first row = high - low, SMA-based RSI, and NaN comparisons evaluating False.

The bot decides once per hour on frames that include the still-forming 1h/4h bar (A1). A
scan at time `s` sees the complete bars before the current bucket plus a forming bar built
from the 1m candles with open time < s. Entry is the open of the 1m bar starting at `s`.

Everything here is candles-only (1m OHLCV).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from flowlevels.config import SupplyZoneConfig

MIN_MS = 60_000
HOUR_MS = 3_600_000
H4_MS = 4 * HOUR_MS


# --------------------------------------------------------------------------- bars


@dataclass(frozen=True)
class Bars:
    ts: np.ndarray  # open time ms
    o: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    v: np.ndarray

    def __len__(self) -> int:
        return len(self.ts)

    def sl(self, a: int | None, b: int | None) -> Bars:
        return Bars(*(x[a:b] for x in (self.ts, self.o, self.h, self.lo, self.c, self.v)))


def aggregate(m1: Bars, step: int) -> Bars:
    if len(m1) == 0:
        return m1
    key = m1.ts // step * step
    start = np.r_[0, np.flatnonzero(np.diff(key)) + 1]
    end = np.r_[start[1:], len(key)] - 1
    return Bars(
        key[start],
        m1.o[start],
        np.maximum.reduceat(m1.h, start),
        np.minimum.reduceat(m1.lo, start),
        m1.c[end],
        np.add.reduceat(m1.v, start),
    )


def _concat(a: Bars, b: Bars) -> Bars:
    return Bars(*(np.r_[x, y] for x, y in zip(
        (a.ts, a.o, a.h, a.lo, a.c, a.v), (b.ts, b.o, b.h, b.lo, b.c, b.v), strict=True
    )))  # fmt: skip


class Frames:
    """Live-style frames (n-1 complete bars + the forming bar) at any scan time."""

    def __init__(self, m1: Bars) -> None:
        self.m1 = m1
        self.h1 = aggregate(m1, HOUR_MS)
        self.h4 = aggregate(m1, H4_MS)

    def _frame(self, agg: Bars, step: int, s: int, n: int) -> Bars:
        b0 = s // step * step
        k = int(np.searchsorted(agg.ts, b0, side="left"))  # complete bars: ts < b0
        j0 = int(np.searchsorted(self.m1.ts, b0, side="left"))
        j1 = int(np.searchsorted(self.m1.ts, s, side="left"))  # 1m bars with ts < s
        if j1 > j0:
            forming = aggregate(self.m1.sl(j0, j1), step)
            return _concat(agg.sl(max(0, k - (n - 1)), k), forming)
        return agg.sl(max(0, k - n), k)

    def at(self, s: int, n1: int, n4: int) -> tuple[Bars, Bars]:
        return self._frame(self.h1, HOUR_MS, s, n1), self._frame(self.h4, H4_MS, s, n4)


# --------------------------------------------------------------------------- indicators


def rolling_mean(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        w = np.lib.stride_tricks.sliding_window_view(x, n)
        out[n - 1 :] = w.mean(axis=1)
    return out


def true_range(b: Bars) -> np.ndarray:
    tr: np.ndarray = np.asarray(b.h - b.lo, dtype=np.float64)
    if len(b) > 1:
        pc = b.c[:-1]
        tr = tr.copy()
        tr[1:] = np.maximum.reduce([tr[1:], np.abs(b.h[1:] - pc), np.abs(b.lo[1:] - pc)])
    return tr


def atr_sma(b: Bars, n: int) -> np.ndarray:
    return rolling_mean(true_range(b), n)


def ema(x: np.ndarray, span: int) -> np.ndarray:
    a = 2.0 / (span + 1.0)
    out = np.empty(len(x))
    acc = x[0]
    for i, xi in enumerate(x):
        acc = xi if i == 0 else a * xi + (1 - a) * acc
        out[i] = acc
    return out


def rsi_sma(c: np.ndarray, n: int) -> np.ndarray:
    d = np.r_[np.nan, np.diff(c)]
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    g, lo = rolling_mean(gain, n), rolling_mean(loss, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = g / lo
        out: np.ndarray = 100 - 100 / (1 + rs)
    return out


def _gt(a: float, b: float) -> bool:
    return bool(a > b)  # NaN -> False, as in pandas comparisons


# --------------------------------------------------------------------------- strategy parts


@dataclass(frozen=True)
class SZone:
    top: float
    bottom: float
    strength: int
    ts: int


def find_zones(b4: Bars, kind: str, p: SupplyZoneConfig) -> list[SZone]:
    x = b4.sl(max(0, len(b4) - p.zone_lookback), None)
    atr = atr_sma(x, 14)
    zones = []
    for i in range(1, len(x)):
        o, c = x.o[i], x.c[i]
        body = abs(c - o)
        if kind == "supply":
            if not (c < o and _gt(body, atr[i] * p.zone_body_atr)):
                continue
            top, bottom = o, c
            later = slice(i + 1, None)
            strength = int(np.sum((x.h[later] >= bottom) & (x.c[later] < bottom)))
        else:
            if not (c > o and _gt(body, atr[i] * p.zone_body_atr)):
                continue
            bottom, top = o, c
            later = slice(i + 1, None)
            strength = int(np.sum((x.lo[later] <= top) & (x.c[later] > top)))
        zones.append(SZone(float(top), float(bottom), strength, int(x.ts[i])))
    return sorted(zones, key=lambda z: z.strength, reverse=True)


def price_in_zone(price: float, zones: list[SZone], tol_pct: float) -> SZone | None:
    for z in zones:
        tol = price * tol_pct / 100
        if z.bottom - tol <= price <= z.top + tol:
            return z
    return None


def vp_poc(b: Bars, bins: int) -> float:
    lo, hi = float(b.lo.min()), float(b.h.max())
    if lo == hi:
        return lo
    edges = np.linspace(lo, hi, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    vol = np.zeros(bins)
    for cl, ch, cv in zip(b.lo, b.h, b.v, strict=True):
        rng = ch - cl
        if rng == 0:
            idx = int(np.searchsorted(edges[1:], cl, side="right"))
            if 0 <= idx < bins:
                vol[idx] += cv
        else:
            ol = np.maximum(cl, edges[:-1])
            oh = np.minimum(ch, edges[1:])
            m = ol < oh
            vol[m] += cv * ((oh[m] - ol[m]) / rng)
    return float(centers[int(np.argmax(vol))])


@dataclass(frozen=True)
class FVG:
    kind: str
    top: float
    bottom: float
    idx: int


def active_fvgs(b: Bars, p: SupplyZoneConfig) -> list[FVG]:
    out = []
    for i in range(2, len(b)):
        if b.lo[i - 2] > b.h[i]:
            top, bot = b.lo[i - 2], b.h[i]
            if (top - bot) / ((top + bot) / 2) * 100 >= p.fvg_min_gap_pct:
                out.append(FVG("bearish", float(top), float(bot), i))
        if b.h[i - 2] < b.lo[i]:
            bot, top = b.h[i - 2], b.lo[i]
            if (top - bot) / ((top + bot) / 2) * 100 >= p.fvg_min_gap_pct:
                out.append(FVG("bullish", float(top), float(bot), i))
    cur = len(b) - 1
    price = float(b.c[-1])
    act = []
    for f in out:
        if cur - f.idx > p.fvg_max_age_bars:
            continue
        if price >= f.top:
            fill = 100.0
        elif price <= f.bottom:
            fill = 0.0
        else:
            fill = (price - f.bottom) / (f.top - f.bottom) * 100
        if fill < p.fvg_fill_threshold_pct:
            act.append(f)
    return act


def price_in_fvg(price: float, fvgs: list[FVG], kind: str) -> bool:
    return any(f.kind == kind and f.bottom <= price <= f.top for f in fvgs)


def recalc_index(f1: Bars, f4: Bars, interval: int) -> int:
    """Index of the last context recalculation in generate_signals (A2)."""
    last, have = -1, False
    for i in range(1, len(f1)):
        if i - last >= interval or not have:
            if not (f4.ts <= f1.ts[i]).any():
                continue
            last, have = i, True
    return last


@dataclass
class Core:
    direction: str
    entry: float
    sl: float
    tp: float
    zone: SZone
    confluence: int
    poc: float


def core_signal(f1: Bars, f4: Bars, direction: str, p: SupplyZoneConfig) -> Core | None:
    """SupplyZoneShortStrategy.generate_signals(...).iloc[-1] for one direction."""
    if len(f1) < 2:
        return None
    r = recalc_index(f1, f4, p.recalc_interval)
    if r < 0:
        return None
    ctx4 = f4.sl(None, int(np.searchsorted(f4.ts, f1.ts[r], side="right")))
    kind = "supply" if direction == "short" else "demand"
    zones = find_zones(ctx4, kind, p)
    poc = vp_poc(f1.sl(max(0, r - p.vp_lookback_bars), r + 1), p.vp_bins)
    fv = active_fvgs(f1.sl(max(0, r - p.fvg_max_age_bars), r + 1), p)
    i = len(f1) - 1
    price, op = float(f1.c[i]), float(f1.o[i])
    atr = float(atr_sma(f1, p.atr_period)[i])
    rsi = float(rsi_sma(f1.c, p.rsi_period)[i])
    z = price_in_zone(price, zones, p.zone_tolerance_pct)
    if z is None:
        return None
    mid = (z.top + z.bottom) / 2
    conf = 1 + int(abs(poc - mid) / mid * 100 <= p.vp_poc_tolerance_pct)
    if direction == "short":
        conf += int(price_in_fvg(price, fv, "bearish"))
        if not (op > price or _gt(rsi, p.rsi_short_min)):
            return None
        sl = z.top + atr * p.atr_sl_mult
        tp = z.bottom - atr * p.atr_tp_mult
        risk, reward = sl - price, price - tp
    else:
        conf += int(price_in_fvg(price, fv, "bullish"))
        if not (price > op or _gt(p.rsi_long_max, rsi)):
            return None
        sl = z.bottom - atr * p.atr_sl_mult
        tp = z.top + atr * p.atr_tp_mult * p.long_tp_factor
        risk, reward = price - sl, tp - price
    if not (_gt(reward, 0) and _gt(risk, 0) and reward / risk >= p.min_rr):
        return None
    return Core(direction, price, float(sl), float(tp), z, conf, poc)


def regime(f4: Bars, threshold: float, p: SupplyZoneConfig) -> str:
    c = f4.c.astype(float)
    e20, e50 = ema(c, p.regime_ema_fast), ema(c, p.regime_ema_slow)
    k = p.regime_slope_bars
    slope = (e50[-1] / e50[-1 - k] - 1) / k * 100 if len(c) > k else 0.0
    if not np.isfinite(slope):
        slope = 0.0
    if e20[-1] > e50[-1] and slope > threshold:
        return "BULL"
    if e20[-1] < e50[-1] and slope < -threshold:
        return "BEAR"
    return "CONSOLIDATION"


def filter_reason(f1: Bars, core: Core, p: SupplyZoneConfig) -> str | None:
    """signal_checker filters in order; None = all passed."""
    v = f1.v
    lb = p.volume_spike_lookback
    if len(v) < lb + 1:
        return "filter_no_volume_spike"
    mean = float(v[-(lb + 1) : -1].mean())
    ratio = max(float(v[-1]), float(v[-2])) / max(mean, 1e-9)
    if ratio < p.volume_spike_multiplier:
        return "filter_no_volume_spike"
    age = p.local_extreme_max_age
    for lookback, name in (
        (p.local_extreme_lookback, "filter_no_local_extreme"),
        (p.multiday_extreme_lookback, "filter_no_multiday_extreme"),
    ):
        if len(f1) < max(lookback, age):
            return name
        if core.direction == "long":
            if float(f1.lo[-age:].min()) > float(f1.lo[-lookback:].min()):
                return name
        elif float(f1.h[-age:].max()) < float(f1.h[-lookback:].max()):
            return name
    if len(f1) < max(p.ema_distance_period, 15):
        return "filter_too_close_to_ema"
    e = ema(f1.c.astype(float), p.ema_distance_period)[-1]
    atr14 = float(atr_sma(f1, 14)[-1])
    dist = abs(float(f1.c[-1]) - e) / (max(atr14, 1e-9) if np.isfinite(atr14) else np.nan)
    if _gt(p.ema_distance_atr_multiplier, dist):
        return "filter_too_close_to_ema"
    return None


def engine_levels(
    direction: str, entry: float, zone: SZone, f4: Bars, p: SupplyZoneConfig
) -> tuple[str, float, float, float, float]:
    """(reason, sl, tp1, tp2, atr4) per executor.calculate_professional_sl/partial_tp and the
    final RR gate. reason 'signal' when all pass."""
    a = atr_sma(f4, p.sl_atr_period)[-1]
    atr = float(a) if np.isfinite(a) else 0.0
    rec = f4.sl(max(0, len(f4) - p.swing_lookback), None)
    nan = float("nan")
    if len(rec) == 0 or atr <= 0:
        return "sl_calculation_failed", nan, nan, nan, atr
    if direction == "short":
        sl = max(
            zone.top + atr * p.sl_zone_buffer_atr, float(rec.h.max()) + atr * p.sl_swing_buffer_atr
        )
        if sl <= entry:
            return "sl_calculation_failed", nan, nan, nan, atr
        tp1 = zone.top - (zone.top - zone.bottom) * 0.5
        tp2 = zone.bottom - atr * p.tp2_atr_mult
    else:
        sl = min(
            zone.bottom - atr * p.sl_zone_buffer_atr,
            float(rec.lo.min()) - atr * p.sl_swing_buffer_atr,
        )
        if sl >= entry:
            return "sl_calculation_failed", nan, nan, nan, atr
        tp1 = zone.bottom + (zone.top - zone.bottom) * 0.5
        tp2 = zone.top + atr * p.tp2_atr_mult
    rr = abs(entry - tp2) / max(abs(entry - sl), 1e-9)  # abs() as in the live code (A13)
    if rr < p.partial_tp_min_rr:
        return "insufficient_rr", sl, nan, nan, atr
    if rr < p.min_rr_required:
        return "filter_rr_too_low", sl, tp1, tp2, atr
    return "signal", sl, tp1, tp2, atr


@dataclass
class Scan:
    ts: int
    regime: str
    direction: str | None
    reason: str
    core: Core | None = None
    sl: float = float("nan")
    tp1: float = float("nan")
    tp2: float = float("nan")
    extra: dict[str, float] = field(default_factory=dict)


def scan(frames: Frames, s: int, threshold: float, p: SupplyZoneConfig) -> Scan:
    """One live scan (engine._check_instrument without account-level gates)."""
    f1, f4 = frames.at(s, p.n_1h, p.n_4h)
    return scan_frames(f1, f4, s, threshold, p)


def scan_frames(f1: Bars, f4: Bars, s: int, threshold: float, p: SupplyZoneConfig) -> Scan:
    if len(f1) < p.min_1h or len(f4) == 0:
        return Scan(s, "", None, "insufficient_candles")
    reg = regime(f4, threshold, p)
    if reg == "CONSOLIDATION":
        return Scan(s, reg, None, "consolidation_regime")
    direction = "short" if reg == "BULL" else "long"
    if len(f4) < p.min_4h:
        return Scan(s, reg, direction, "insufficient_data")
    core = core_signal(f1, f4, direction, p)
    if core is None:
        return Scan(s, reg, direction, "no_strategy_signal")
    fr = filter_reason(f1, core, p)
    if fr is not None:
        return Scan(s, reg, direction, fr, core)
    reason, sl, tp1, tp2, atr4 = engine_levels(direction, core.entry, core.zone, f4, p)
    return Scan(s, reg, direction, reason, core, sl, tp1, tp2, {"atr4": atr4})


# --------------------------------------------------------------------------- trades


@dataclass
class SZTrade:
    symbol: str
    direction: int
    signal_ts: int
    entry_ts: int
    entry: float
    sl: float
    tp1: float
    tp2: float
    exit_ts: int
    exit_px: float  # size-weighted average exit price of both legs
    x1: float
    x2: float
    kind1: str
    kind2: str
    r_unit: float
    zone_top: float
    zone_bottom: float
    confluence: int
    window_end_close: bool


def _trail(frames: Frames, s: int, direction: int, p: SupplyZoneConfig) -> float:
    f1, _ = frames.at(s, p.n_1h, p.n_4h)
    atr = float(atr_sma(f1, p.sl_atr_period)[-1])
    if not np.isfinite(atr) or atr <= 0:
        return float("nan")
    rec = f1.sl(max(0, len(f1) - p.chandelier_lookback), None)
    if direction > 0:
        return float(rec.h.max()) - atr * p.chandelier_atr_mult
    return float(rec.lo.min()) + atr * p.chandelier_atr_mult


def manage(
    frames: Frames, sc: Scan, symbol: str, end_ms: int, p: SupplyZoneConfig
) -> SZTrade | None:
    """Simulate one trade on the 1m path from the scan time to its close (or end_ms)."""
    m1 = frames.m1
    s = sc.ts
    j = int(np.searchsorted(m1.ts, s, side="left"))
    if j >= len(m1) or m1.ts[j] != s or sc.core is None:
        return None
    d = 1 if sc.direction == "long" else -1
    entry = float(m1.o[j])
    shift = entry - sc.core.entry
    sl, tp1, tp2 = sc.sl + shift, sc.tp1 + shift, sc.tp2 + shift
    r_unit = abs(entry - sl)
    stop = sl
    open1, open2 = True, True
    x1 = x2 = float("nan")
    k1 = k2 = ""
    tp1_hit_unprocessed = False
    trailing_sl: float | None = None
    t1 = t2 = 0
    k = j
    while k < len(m1) and m1.ts[k] < end_ms and (open1 or open2):
        t = int(m1.ts[k])
        if t > s and (t - p.scan_delay_s * 1000) % HOUR_MS == 0:  # hourly tick
            if tp1_hit_unprocessed:
                tp1_hit_unprocessed = False
                stop = entry
                trailing_sl = entry
            if trailing_sl is not None and open2:
                nt = _trail(frames, t, d, p)
                if np.isfinite(nt) and d * (nt - trailing_sl) > 0:
                    trailing_sl = nt
                    stop = nt
        o, h, lo = float(m1.o[k]), float(m1.h[k]), float(m1.lo[k])
        hit_stop = (lo <= stop) if d > 0 else (h >= stop)
        if hit_stop:
            fill = min(o, stop) if d > 0 else max(o, stop)
            if open1:
                x1, k1, open1, t1 = fill, "stop", False, t
            if open2:
                x2, k2, open2, t2 = fill, "stop" if trailing_sl is None else "trail", False, t
            break
        for leg in (1, 2):
            tp = tp1 if leg == 1 else tp2
            if not (open1 if leg == 1 else open2):
                continue
            if (h >= tp) if d > 0 else (lo <= tp):
                fill = max(o, tp) if d > 0 else min(o, tp)
                if leg == 1:
                    x1, k1, open1, t1 = fill, "tp1", False, t
                    tp1_hit_unprocessed = True
                else:
                    x2, k2, open2, t2 = fill, "tp2", False, t
        k += 1
    eoc = False
    if open1 or open2:
        kk = max(j, min(k, len(m1)) - 1)
        px, t = float(m1.c[kk]), int(m1.ts[kk])
        if open1:
            x1, k1, t1 = px, "window_end", t
        if open2:
            x2, k2, t2 = px, "window_end", t
        eoc = True
    w1 = p.tp1_size
    return SZTrade(
        symbol, d, s, s, entry, sl, tp1, tp2, max(t1, t2), w1 * x1 + (1 - w1) * x2, x1, x2,
        k1, k2, r_unit, sc.core.zone.top, sc.core.zone.bottom, sc.core.confluence, eoc,
    )  # fmt: skip


def run_symbol(
    frames: Frames,
    symbol: str,
    start_ms: int,
    end_ms: int,
    threshold: float,
    p: SupplyZoneConfig,
) -> tuple[list[SZTrade], list[Scan]]:
    """Hourly scans in [start_ms, end_ms); one position per symbol; 48 h cooldown after a
    close. Trades still open at end_ms are closed at the last close before it (D-039)."""
    trades: list[SZTrade] = []
    scans: list[Scan] = []
    s = (start_ms // HOUR_MS) * HOUR_MS + p.scan_delay_s * 1000
    if s < start_ms:
        s += HOUR_MS
    busy_until = -1
    cooldown_until = -1
    while s < end_ms:
        if s <= busy_until or s < cooldown_until:
            s += HOUR_MS
            continue
        sc = scan(frames, s, threshold, p)
        scans.append(sc)
        if sc.reason == "signal":
            tr = manage(frames, sc, symbol, end_ms, p)
            if tr is not None:
                trades.append(tr)
                busy_until = tr.exit_ts
                cooldown_until = tr.exit_ts + 60_000 + p.cooldown_hours * HOUR_MS
        s += HOUR_MS
    return trades, scans
