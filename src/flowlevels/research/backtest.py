"""Tuning-window research run (Phase 3): touches, detectors, event groups, trades.

One code path with live: the inputs are the stored 1m footprint/OHLCV built by the
Ingestor, levels come from `levels.engine` (static zones at the session open), bar features
and detectors from `signals`. Nothing at time t uses data at or after t:
- static zones: rows closed by the session open; dynamic levels: rows closed by the bar
  open; detector flags of a bar: known at its close; entries at the next bar's open.

Event groups (SPEC 9, D-035), per touch and per grid config (W, threshold):
- treatment: the weighted count of distinct detectors that fired (reversal direction, on
  bars of the window that intersect the zone) reaches the threshold at window bar k < W;
  entry at the close of bar k, stop beyond that bar's extreme (+ buffer);
- treatment_zonestop: the same entries with the zone-edge stop (cost-geometry sensitivity);
- unconfirmed: the threshold is not reached within W bars; entry at the close of window
  bar W-1, zone-edge stop; if the stop was already touched inside the window the touch is
  "invalidated" (counted, no trade);
- baseline (information): every touch, entry at the touch bar close, zone-edge stop;
- random: random bars inside static zones (assigned by the same nearest-anchor rule),
  matched on count and hour of day to the baseline trades, random direction, zone-edge stop.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import polars as pl

from flowlevels.config import AppConfig
from flowlevels.data.quality import Exclusions, Hole, bar_mask, clean_sessions, load_holes
from flowlevels.data.storage import ParquetStore, day_start_ms
from flowlevels.footprint.bars import res_ms
from flowlevels.levels.engine import (
    DAY_MS,
    HOUR_MS,
    atr_1h,
    history_start_ms,
    session_zones,
    static_levels,
)
from flowlevels.levels.report import load_range
from flowlevels.research.dynamic import DYNAMIC_NAMES, dynamic_series
from flowlevels.research.touches import (
    SessionZones,
    dynamic_confluence,
    dynamic_touches,
    static_touches,
)
from flowlevels.research.trades import Path1m, plan_stop
from flowlevels.signals.detectors import DETECTORS, detector_flags
from flowlevels.signals.features import add_baselines, bar_features

log = logging.getLogger(__name__)

MINUTE_MS = 60_000
EXITS_EXTRA = ("zone", "time")


def exits(cfg: AppConfig) -> list[str]:
    return [f"{k:g}R" for k in cfg.trade_model.targets_r] + list(EXITS_EXTRA)


def tuning_days(store: ParquetStore, symbols: list[str], n: int) -> list[dt.date]:
    """The last n days that are complete for every symbol."""
    common: set[dt.date] | None = None
    for s in symbols:
        done = {d for d in store.days("ohlcv_1m", s) if store.completion("ohlcv_1m", s, d)}
        common = done if common is None else common & done
    days = sorted(common or [])
    if len(days) < n:
        raise ValueError(f"only {len(days)} complete days stored, need {n} (run make bootstrap)")
    return days[-n:]


def hourly_context(oh: pl.DataFrame, lookback_h: int, k: float, atr_period: int) -> pl.DataFrame:
    """Regime per complete hour h (known at h + 1h): up / down / range (2 parameters)."""
    h = (
        oh.with_columns((pl.col("minute_ts") // HOUR_MS * HOUR_MS).alias("h"))
        .group_by("h")
        .agg(pl.col("high").max(), pl.col("low").min(), pl.col("close").last())
        .sort("h")
    )
    pc = pl.col("close").shift(1)
    tr = pl.max_horizontal(
        pl.col("high") - pl.col("low"), (pl.col("high") - pc).abs(), (pl.col("low") - pc).abs()
    )
    h = h.with_columns(tr.rolling_mean(atr_period).alias("atr"))
    mv = pl.col("close") - pl.col("close").shift(lookback_h)
    lab = (
        pl.when(mv > k * pl.col("atr"))
        .then(pl.lit("up"))
        .when(mv < -k * pl.col("atr"))
        .then(pl.lit("down"))
        .otherwise(pl.lit("range"))
    )
    return h.select((pl.col("h") + HOUR_MS).alias("known_ts"), lab.alias("context")).drop_nulls()


@dataclass
class SymbolData:
    symbol: str
    oh: pl.DataFrame
    fp: pl.DataFrame
    sessions: dict[int, SessionZones]
    dyn: pl.DataFrame
    path: Path1m
    context: pl.DataFrame
    holes: list[Hole]


def load_symbol(
    store: ParquetStore, cfg: AppConfig, symbol: str, days: list[dt.date], excl: Exclusions
) -> SymbolData:
    sc, lc = cfg.symbol(symbol), cfg.levels
    clean = clean_sessions(store, symbol, days, lc, excl)
    d_first = day_start_ms(days[0])
    start = min(history_start_ms(d_first, lc), d_first - cfg.footprint.baseline_days * DAY_MS)
    end = day_start_ms(days[-1]) + DAY_MS - 1
    oh, fp = load_range(store, symbol, start, end)
    sessions: dict[int, SessionZones] = {}
    for d in clean:
        d0 = day_start_ms(d)
        h0 = history_start_ms(d0, lc)
        o = oh.filter((pl.col("minute_ts") >= h0) & (pl.col("minute_ts") < d0))
        f = fp.filter((pl.col("minute_ts") >= h0) & (pl.col("minute_ts") < d0))
        atr = atr_1h(o, d0, lc.atr_period_1h)
        if not atr:
            continue
        sessions[d0] = SessionZones(
            d0, atr, session_zones(symbol, d0, static_levels(o, f, d0, sc, lc), atr, sc, lc)
        )
    dyn = dynamic_series(oh, sc, lc, cfg.signals.dynamic_min_minutes)
    path = Path1m(*(oh[c].to_numpy() for c in ("minute_ts", "open", "high", "low", "close")))
    ctx = hourly_context(oh, cfg.context.lookback_h, cfg.context.trend_k_atr, lc.atr_period_1h)
    return SymbolData(symbol, oh, fp, sessions, dyn, path, ctx, load_holes(store, symbol))


@dataclass
class ResData:
    bars: pl.DataFrame
    dyn_at_bar: pl.DataFrame
    touches: pl.DataFrame
    masks: np.ndarray  # (n_touches, max_w) detector bitmask per window bar, in-zone only


def _bit(d: str) -> int:
    return 1 << DETECTORS.index(d)


def prepare_res(sd: SymbolData, cfg: AppConfig, res: str) -> ResData:
    sc = cfg.symbol(sd.symbol)
    bars = bar_features(sd.oh, sd.fp, res, cfg.footprint)
    step = res_ms(res)
    excluded = bar_mask(bars["bar_ts"], step, sd.holes) if sd.holes else None
    bars = add_baselines(bars, cfg.footprint, excluded=excluded)
    bars = detector_flags(bars, cfg.signals)
    dyn_at_bar = bars.select("bar_ts").join_asof(
        sd.dyn.rename({"minute_ts": "bar_ts"}), on="bar_ts", strategy="backward"
    )
    st = static_touches(bars, sd.sessions, sd.symbol, res)
    st = dynamic_confluence(st, dyn_at_bar, DYNAMIC_NAMES)
    dy = dynamic_touches(
        bars,
        dyn_at_bar,
        DYNAMIC_NAMES,
        sd.sessions,
        cfg.levels.zone_tolerance_k_atr,
        sd.symbol,
        res,
    ).with_columns(pl.lit([], dtype=pl.List(pl.String)).alias("dyn_confluence"))
    touches = pl.concat([st, dy])
    if sd.holes:  # unrepaired holes: drop touches whose window or hold overlaps one
        w = max(cfg.research.grid_window_bars)
        hold = cfg.trade_model.max_hold_minutes * MINUTE_MS
        bad = pl.lit(False)
        for h in sd.holes:
            bad = bad | (
                (pl.col("bar_ts") - w * step < h.before_ms)
                & (pl.col("bar_ts") + (w + 1) * step + hold > h.after_ms)
            )
        touches = touches.filter(~bad)
    ctx = sd.context.rename({"known_ts": "bar_ts"})
    touches = touches.sort("bar_ts").join_asof(ctx, on="bar_ts", strategy="backward")
    touches = touches.sort("pool", "bar_ts", "zone_id")
    masks = window_masks(
        bars, dyn_at_bar, touches, max(cfg.research.grid_window_bars), sd.sessions, cfg
    )
    del sc
    return ResData(bars, dyn_at_bar, touches, masks)


def window_masks(
    bars: pl.DataFrame,
    dyn_at_bar: pl.DataFrame,
    touches: pl.DataFrame,
    max_w: int,
    sessions: dict[int, SessionZones],
    cfg: AppConfig,
) -> np.ndarray:
    n = bars.height
    lo, hi = bars["low"].to_numpy(), bars["high"].to_numpy()
    flags = {
        (d, s): bars[f"{d}_{'long' if s > 0 else 'short'}"].to_numpy()
        for d in DETECTORS
        for s in (1, -1)
    }
    lv = dyn_at_bar.select(DYNAMIC_NAMES).to_numpy().astype(np.float64)
    name_idx = {nm: i for i, nm in enumerate(DYNAMIC_NAMES)}
    out = np.zeros((touches.height, max_w), dtype=np.int64)
    tk = cfg.levels.zone_tolerance_k_atr
    for i, t in enumerate(touches.iter_rows(named=True)):
        p0, d = int(t["pos"]), int(t["direction"])
        for k in range(max_w):
            p = p0 + k
            if p >= n:
                break
            if t["pool"] == "static":
                zl, zh = t["zone_low"], t["zone_high"]
            else:
                c = lv[p, name_idx[t["anchor_type"]]]
                if not np.isfinite(c):
                    continue
                tol = tk * t["atr_open"]
                zl, zh = c - tol, c + tol
            if not (lo[p] <= zh and hi[p] >= zl):
                continue  # detectors only while price is inside the zone
            m = 0
            for det in DETECTORS:
                if flags[(det, d)][p]:
                    m |= _bit(det)
            out[i, k] = m
    return out


def signal_bar(masks: np.ndarray, w: int, thr: float, weights: dict[str, float]) -> np.ndarray:
    """First window bar k < w where the weighted count of distinct detectors >= thr; -1 if none."""
    cum = np.bitwise_or.accumulate(masks[:, :w], axis=1)
    score = np.zeros(cum.shape)
    for det in DETECTORS:
        score += weights.get(det, 0.0) * ((cum & _bit(det)) > 0)
    hit = score >= thr - 1e-12
    return np.where(hit.any(axis=1), hit.argmax(axis=1), -1)


@dataclass
class TradeBook:
    """Compact store: unique simulations, touch attributes once per touch, and small row
    tuples (group, w, thr, touch, exit, sim, detectors, entry_ts) referencing them."""

    rows: list[tuple[str, int, float, int, str, int, int, int]] = field(default_factory=list)
    sims: list[dict[str, Any]] = field(default_factory=list)
    cache: dict[tuple[int, int, float, str], int] = field(default_factory=dict)
    touches: dict[int, dict[str, Any]] = field(default_factory=dict)

    def frame(self, symbol: str, res: str) -> pl.DataFrame:
        rows = pl.DataFrame(
            self.rows,
            schema={
                "group": pl.String, "w": pl.Int64, "thr": pl.Float64, "touch": pl.Int64,
                "exit": pl.String, "sim": pl.Int64, "detectors": pl.Int64, "entry_ts": pl.Int64,
            },
            orient="row",
        )  # fmt: skip
        sims = pl.DataFrame(self.sims, infer_schema_length=None).with_row_index("sim")
        sims = sims.with_columns(pl.col("sim").cast(pl.Int64))
        tch = pl.DataFrame(
            [{"touch": k, **v} for k, v in self.touches.items()], infer_schema_length=None
        )
        cat = ["group", "exit", "pool", "anchor_type", "context", "zone_id", "exit_kind"]
        return (
            rows.join(sims.drop("reason", strict=False), on="sim", how="left")
            .join(tch, on="touch", how="left")
            .with_columns(pl.lit(symbol).alias("symbol"), pl.lit(res).alias("res"))
            .with_columns(pl.col(c).cast(pl.Categorical) for c in [*cat, "symbol", "res"])
        )


def _next_zone(sz: SessionZones, entry: float, direction: int) -> float | None:
    if direction > 0:
        c = [z.low for z in sz.zones if z.low > entry]
        return min(c) if c else None
    c = [z.high for z in sz.zones if z.high < entry]
    return max(c) if c else None


def _sim(
    sd: SymbolData,
    cfg: AppConfig,
    book: TradeBook,
    entry_ts: int,
    direction: int,
    stop: float,
    min_stop: float,
    exit_name: str,
    session: int,
) -> int:
    key = (entry_ts, direction, round(stop, 10), exit_name)
    if key in book.cache:
        return book.cache[key]
    book.cache[key] = len(book.sims)
    book.sims.append(
        _simulate_one(sd, cfg, entry_ts, direction, stop, min_stop, exit_name, session)
    )
    return book.cache[key]


def _simulate_one(
    sd: SymbolData,
    cfg: AppConfig,
    entry_ts: int,
    direction: int,
    stop: float,
    min_stop: float,
    exit_name: str,
    session: int,
) -> dict[str, Any]:
    p = sd.path
    i0 = p.index(entry_ts)
    if i0 is None:
        return {"valid": False, "reason": "no_entry_minute"}
    entry = float(p.open[i0])
    stop2, r = plan_stop(entry, stop, direction, min_stop)
    if r <= 0:
        return {"valid": False, "reason": "stop_beyond_entry"}
    nz = _next_zone(sd.sessions[session], entry, direction) if exit_name == "zone" else None
    from flowlevels.research.trades import Costs, simulate

    f = simulate(
        p,
        entry_ts,
        direction,
        stop,
        min_stop,
        exit_name,
        nz,
        cfg.trade_model.max_hold_minutes,
        Costs(0.0, 0.0, 0.0),
    )
    res = {
        "valid": f.valid,
        "reason": f.reason,
        "entry": f.entry,
        "stop": f.stop,
        "r_unit": f.r_unit,
        "exit_ts": f.exit_ts,
        "exit_px": f.exit_px,
        "exit_kind": f.exit_kind,
        "gross_r": f.gross_r,
        "mfe_r": f.mfe_r,
        "mae_r": f.mae_r,
    }
    del stop2
    return res


def evaluate(
    sd: SymbolData, rd: ResData, cfg: AppConfig, res: str, book: TradeBook, seed: int
) -> dict[str, Any]:
    """Append trade rows for every group and grid config. Returns counters."""
    tm, rc = cfg.trade_model, cfg.research
    ex_tick = cfg.costs.execution_instruments[sd.symbol].tick
    buf = tm.stop_buffer_ticks * ex_tick
    step = res_ms(res)
    bars = rd.bars
    lo, hi = bars["low"].to_numpy(), bars["high"].to_numpy()
    bts = bars["bar_ts"].to_numpy()
    n = bars.height
    touches = rd.touches
    counters: dict[str, int] = {"touches": touches.height, "invalidated": 0, "beyond_data": 0}
    ex = exits(cfg)
    lv = rd.dyn_at_bar.select(DYNAMIC_NAMES).to_numpy().astype(np.float64)
    name_idx = {nm: i for i, nm in enumerate(DYNAMIC_NAMES)}

    def zone_at(t: dict[str, Any], p: int) -> tuple[float, float]:
        if t["pool"] == "static":
            return t["zone_low"], t["zone_high"]
        c = lv[p, name_idx[t["anchor_type"]]]
        tol = cfg.levels.zone_tolerance_k_atr * t["atr_open"]
        return (c - tol, c + tol) if np.isfinite(c) else (t["zone_low"], t["zone_high"])

    def zone_stop(t: dict[str, Any], p: int) -> float:
        zl, zh = zone_at(t, p)
        return zl - buf if t["direction"] > 0 else zh + buf

    def add(
        group: str,
        t: dict[str, Any],
        tid: int,
        w: int,
        thr: float,
        p_sig: int,
        stop: float,
        dets: int,
    ) -> None:
        if p_sig + 1 > n:
            counters["beyond_data"] += 1
            return
        entry_ts = int(bts[p_sig]) + step
        min_stop = tm.min_stop_k_atr * t["atr_open"]
        for e in ex:
            sim = _sim(sd, cfg, book, entry_ts, t["direction"], stop, min_stop, e, t["session"])
            book.rows.append((group, w, thr, tid, e, sim, dets, entry_ts))
        if tid not in book.touches:
            book.touches[tid] = {
                "pool": t["pool"],
                "session": t["session"],
                "touch_ts": t["bar_ts"],
                "direction": t["direction"],
                "zone_id": t.get("zone_id"),
                "anchor_type": t["anchor_type"],
                "touch_n": t["touch_n"],
                "n_members": t["n_members"],
                "context": t["context"],
                "dyn_conf": len(t["dyn_confluence"] or []) > 0,
            }

    weights = cfg.signals.weights
    for w in rc.grid_window_bars:
        for thr in rc.grid_score_threshold:
            sig = signal_bar(rd.masks, w, thr, weights)
            cum = np.bitwise_or.accumulate(rd.masks[:, :w], axis=1)
            for i, t in enumerate(touches.iter_rows(named=True)):
                p0 = int(t["pos"])
                tid = i
                k = int(sig[i])
                if k >= 0:
                    p = p0 + k
                    stop = lo[p] - buf if t["direction"] > 0 else hi[p] + buf
                    dets = int(cum[i, k])
                    add("treatment", t, tid, w, thr, p, stop, dets)
                    add("treatment_zonestop", t, tid, w, thr, p, zone_stop(t, p), dets)
                else:
                    p_end = p0 + w - 1
                    if p_end >= n:
                        counters["beyond_data"] += 1
                        continue
                    zs = zone_stop(t, p0)
                    seg_lo, seg_hi = lo[p0 : p_end + 1], hi[p0 : p_end + 1]
                    breached = (seg_lo <= zs).any() if t["direction"] > 0 else (seg_hi >= zs).any()
                    if breached:
                        counters["invalidated"] += 1
                        continue
                    add("unconfirmed", t, tid, w, thr, p_end, zs, 0)
    # baseline: every touch, entry at the touch-bar close, zone-edge stop
    for i, t in enumerate(touches.iter_rows(named=True)):
        p0 = int(t["pos"])
        add("baseline", t, i, 0, 0.0, p0, zone_stop(t, p0), 0)
    # random control (static pool): matched on count and hour of day to baseline trades
    rnd = random_control(sd, rd, cfg, res, seed)
    for j, t in enumerate(rnd):
        add("random", t, -1 - j, 0, 0.0, int(t["pos"]), zone_stop(t, int(t["pos"])), 0)
    counters["random"] = len(rnd)
    return counters


def random_control(
    sd: SymbolData, rd: ResData, cfg: AppConfig, res: str, seed: int
) -> list[dict[str, Any]]:
    from flowlevels.research.touches import _assign

    bars = rd.bars
    ts = bars["bar_ts"].to_numpy()
    lo, hi, cl = (bars[c].to_numpy() for c in ("low", "high", "close"))
    prev_c = np.r_[np.nan, cl[:-1]]
    day = ts // DAY_MS * DAY_MS
    cand: list[tuple[int, Any, int]] = []  # (pos, zone, session)
    for d0, sz in sd.sessions.items():
        if not sz.zones:
            continue
        idx = np.flatnonzero((day == d0) & np.isfinite(prev_c))
        if not len(idx):
            continue
        z_lo = np.array([z.low for z in sz.zones])
        z_hi = np.array([z.high for z in sz.zones])
        anc = np.array([next(m.price for m in z.members if m.name == z.anchor) for z in sz.zones])
        a = _assign(
            lo[idx], hi[idx], prev_c[idx], z_lo, z_hi, anc, np.array([z.score for z in sz.zones])
        )
        cand += [(int(p), sz.zones[int(k)], d0) for p, k in zip(idx, a, strict=True) if k >= 0]
    st = rd.touches.filter(pl.col("pool") == "static")
    if not cand or st.height == 0:
        return []
    rng = np.random.default_rng(seed)
    hours = (st["bar_ts"].to_numpy() % DAY_MS) // HOUR_MS
    by_hour: dict[int, list[int]] = {}
    for j, (p, _, _) in enumerate(cand):
        by_hour.setdefault(int((ts[p] % DAY_MS) // HOUR_MS), []).append(j)
    out = []
    for h in hours:
        pool = by_hour.get(int(h))
        if not pool:
            continue
        p, z, d0 = cand[pool[int(rng.integers(len(pool)))]]
        out.append(
            {
                "pos": p,
                "bar_ts": int(ts[p]),
                "pool": "static",
                "zone_low": z.low,
                "zone_high": z.high,
                "direction": int(rng.choice([-1, 1])),
                "anchor_type": next(m.type for m in z.members if m.name == z.anchor),
                "touch_n": 0,
                "n_members": len(z.members),
                "context": None,
                "dyn_confluence": [],
                "session": d0,
                "atr_open": sd.sessions[d0].atr_open,
            }
        )
    return out


def run(
    store: ParquetStore, cfg: AppConfig, symbols: list[str]
) -> tuple[pl.DataFrame, dict[str, Any]]:
    t0 = time.perf_counter()
    days = tuning_days(store, symbols, cfg.research.tuning_days)
    excl = Exclusions(holes=[])
    meta: dict[str, Any] = {
        "days": [days[0].isoformat(), days[-1].isoformat()],
        "n_days": len(days),
        "counters": {},
        "sessions": {},
        "timing_s": {},
    }
    frames = []
    for i, s in enumerate(symbols):
        ts = time.perf_counter()
        sd = load_symbol(store, cfg, s, days, excl)
        meta["sessions"][s] = len(sd.sessions)
        meta["timing_s"][f"{s} load"] = round(time.perf_counter() - ts, 1)
        for res in cfg.signals.resolutions:
            ts = time.perf_counter()
            rd = prepare_res(sd, cfg, res)
            book = TradeBook()
            c = evaluate(sd, rd, cfg, res, book, cfg.research.seed + 1000 * i + len(res))
            meta["counters"][f"{s} {res}"] = c | {"unique_trades": len(book.sims)}
            frames.append(book.frame(s, res))
            del book, rd
            meta["timing_s"][f"{s} {res}"] = round(time.perf_counter() - ts, 1)
            log.info("%s %s: %s in %.1fs", s, res, c, time.perf_counter() - ts)
    meta["exclusions"] = excl.summary()
    meta["seconds_total"] = round(time.perf_counter() - t0, 1)
    trades = pl.concat(frames, how="diagonal_relaxed")
    return trades, meta
