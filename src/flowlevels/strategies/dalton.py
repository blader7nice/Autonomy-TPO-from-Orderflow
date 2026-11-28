"""ITERATION-3 (MASTER-PLAN, Phase B): Dalton Market Profile rules H1 / H2 on 1m candles.

Session = UTC day. Prior-day TPO profile from 1m OHLC (each 30-min period adds 1 to every
TPO row between its low and high, D-016 conventions via `levels.profile`), 70 % value area.
Decisions at 30-min period closes; entry at the next period's first 1m open.

H1 (80 % rule): session opens outside the prior VA; the first time two consecutive periods
close inside [VAL, VAH], enter toward the opposite edge. Stop = re-entered edge beyond by
max(k x ATR(1h), 3 x RT). Target = opposite edge. Time stop = session end. One per session.

H2 (balance fade): session opens inside the prior VA. Per edge, the first period trading
beyond it is the probe; closing back inside = rejection -> fade. Stop = probe extreme +
2 exec ticks, at least max(k x ATR(1h), 3 x RT) from the entry. Target = opposite edge
(POC secondary). Skip if target distance < r x stop distance. One trade per edge per session,
one position per symbol at a time, time stop = session end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import polars as pl

from flowlevels.config import AppConfig
from flowlevels.levels.profile import tpo_profile, value_area
from flowlevels.research.iteration1 import hourly_atr, round_trip_cost
from flowlevels.research.trades import Costs, Path1m, simulate

DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
MIN_MS = 60_000
MONDAY_OFFSET_MS = 4 * DAY_MS  # 1970-01-01 is a Thursday; Monday 1970-01-05


@dataclass(frozen=True)
class Grid:
    rule: str  # H1 | H2
    k_atr: float
    trend: str  # none | vwap_w
    skip_r: float  # H2 only (0 for H1)

    @property
    def key(self) -> str:
        return f"{self.rule}|k{self.k_atr:g}|{self.trend}" + (
            f"|r{self.skip_r:g}" if self.rule == "H2" else ""
        )


def grids() -> list[Grid]:
    out = [Grid("H1", k, t, 0.0) for k in (0.25, 0.5) for t in ("none", "vwap_w")]
    out += [
        Grid("H2", k, t, r) for k in (0.25, 0.5) for t in ("none", "vwap_w") for r in (2.0, 1.5)
    ]
    return out


def weekly_vwap_before(oh: pl.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """(minute_ts, vwap of the current week using minutes strictly before minute_ts)."""
    ts = oh["minute_ts"].to_numpy()
    tp = ((oh["high"] + oh["low"] + oh["close"]) / 3).to_numpy()
    v = oh["volume"].to_numpy()
    week = (ts - MONDAY_OFFSET_MS) // WEEK_MS
    pv, vv = tp * v, v.astype(np.float64)
    cpv, cv = np.cumsum(pv), np.cumsum(vv)
    start = np.r_[0, np.flatnonzero(np.diff(week)) + 1]
    first = np.repeat(start, np.diff(np.r_[start, len(ts)]))
    base_pv = np.where(first > 0, cpv[first - 1], 0.0)
    base_v = np.where(first > 0, cv[first - 1], 0.0)
    # exclusive: minutes before this one in the same week
    ex_pv = np.r_[0.0, cpv[:-1]] - base_pv
    ex_v = np.r_[0.0, cv[:-1]] - base_v
    with np.errstate(divide="ignore", invalid="ignore"):
        vw = np.where(ex_v > 0, ex_pv / ex_v, np.nan)
    return ts, vw


@dataclass
class Session:
    d0: int
    vah: float
    val: float
    poc: float
    open0: float
    p_end: np.ndarray  # period end ts (48)
    p_hi: np.ndarray
    p_lo: np.ndarray
    p_cl: np.ndarray


def sessions(oh: pl.DataFrame, days: list[int], cfg: AppConfig, symbol: str) -> list[Session]:
    sc, lc = cfg.symbol(symbol), cfg.levels
    step = lc.tpo_period_minutes * MIN_MS
    out = []
    ts = oh["minute_ts"].to_numpy()
    for d0 in days:
        a, b = np.searchsorted(ts, [d0 - DAY_MS, d0])
        c = np.searchsorted(ts, d0 + DAY_MS)
        prev, cur = oh[int(a) : int(b)], oh[int(b) : int(c)]
        if prev.height < 1380 or cur.height < 1380 or int(ts[b]) != d0:
            continue  # incomplete day (> 1 h missing) or no 00:00 minute
        va = value_area(
            tpo_profile(prev, lc.tpo_period_minutes, sc.tpo_tick, sc.exchange_tick),
            lc.value_area_pct,
        )
        if va is None:
            continue
        per = (
            cur.with_columns((pl.col("minute_ts") // step * step).alias("p"))
            .group_by("p", maintain_order=True)
            .agg(pl.col("high").max(), pl.col("low").min(), pl.col("close").last())
            .sort("p")
        )
        out.append(
            Session(
                d0, va.vah, va.val, va.poc, float(cur["open"][0]),
                per["p"].to_numpy() + step, per["high"].to_numpy(), per["low"].to_numpy(),
                per["close"].to_numpy(),
            )
        )  # fmt: skip
    return out


@dataclass
class Signal:
    rule: str
    session: int
    decision_ts: int
    direction: int
    edge: str
    raw_stop: float  # H1: final stop level is computed with k; H2: probe extreme + buffer
    target: float
    poc: float
    close: float


def h1_signals(s: Session) -> list[Signal]:
    if s.val <= s.open0 <= s.vah:
        return []
    d = -1 if s.open0 > s.vah else 1
    inside = (s.p_cl >= s.val) & (s.p_cl <= s.vah)
    for p in range(1, len(inside) - 1):  # the last period has no next bar in the session
        if inside[p - 1] and inside[p]:
            edge, tgt = ("VAH", s.val) if d < 0 else ("VAL", s.vah)
            lvl = s.vah if d < 0 else s.val
            return [Signal("H1", s.d0, int(s.p_end[p]), d, edge, lvl, tgt, s.poc, float(s.p_cl[p]))]
    return []


def h2_signals(s: Session, buffer: float) -> list[Signal]:
    if not (s.val <= s.open0 <= s.vah):
        return []
    out: list[Signal] = []
    for edge in ("VAH", "VAL"):
        probe = s.p_hi > s.vah if edge == "VAH" else s.p_lo < s.val
        idx = np.flatnonzero(probe)
        if not len(idx):
            continue
        p = int(idx[0])
        if p >= len(s.p_cl) - 1 or not (s.val <= s.p_cl[p] <= s.vah):
            continue  # acceptance outside (edge done) or no next period
        if edge == "VAH":
            out.append(Signal("H2", s.d0, int(s.p_end[p]), -1, edge, float(s.p_hi[p]) + buffer,
                              s.val, s.poc, float(s.p_cl[p])))  # fmt: skip
        else:
            out.append(Signal("H2", s.d0, int(s.p_end[p]), 1, edge, float(s.p_lo[p]) - buffer,
                              s.vah, s.poc, float(s.p_cl[p])))  # fmt: skip
    # same decision time with opposite directions (an outside bar): skip both (conservative)
    if len(out) == 2 and out[0].decision_ts == out[1].decision_ts:
        return []
    return sorted(out, key=lambda x: x.decision_ts)


def run_symbol(
    oh: pl.DataFrame, days: list[int], cfg: AppConfig, symbol: str
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """All H1/H2 grid trades for one symbol. Each row is one trade x exit."""
    path = Path1m(*(oh[c].to_numpy() for c in ("minute_ts", "open", "high", "low", "close")))
    atr_ts, atr = hourly_atr(oh, cfg.levels.atr_period_1h)
    vw_ts, vw = weekly_vwap_before(oh)
    tick = cfg.costs.execution_instruments[symbol].tick
    buf = cfg.trade_model.stop_buffer_ticks * tick
    ss = sessions(oh, days, cfg, symbol)
    rows: list[dict[str, Any]] = []
    skips: dict[str, int] = {}
    sig = {
        "H1": [x for s in ss for x in h1_signals(s)],
        "H2": [x for s in ss for x in h2_signals(s, buf)],
    }
    for g in grids():
        busy_until = -1
        for x in sig[g.rule]:
            entry_ts = x.decision_ts
            if entry_ts <= busy_until:
                skips[f"{g.key} busy"] = skips.get(f"{g.key} busy", 0) + 1
                continue
            i = path.index(entry_ts)
            j = int(np.searchsorted(atr_ts, entry_ts, side="right")) - 1
            if i is None or j < 0:
                skips[f"{g.key} no_data"] = skips.get(f"{g.key} no_data", 0) + 1
                continue
            if g.trend == "vwap_w":
                w = vw[int(np.searchsorted(vw_ts, entry_ts))]  # VWAP of minutes < decision
                if not np.isfinite(w) or x.direction * (x.close - w) <= 0:
                    skips[f"{g.key} trend"] = skips.get(f"{g.key} trend", 0) + 1
                    continue
            entry = float(path.open[i])
            mind = max(
                g.k_atr * float(atr[j]),
                cfg.iteration1.min_stop_cost_mult * round_trip_cost(cfg, symbol, entry),
            )
            if g.rule == "H1":
                stop, min_stop = x.raw_stop - x.direction * mind, 0.0
            else:
                stop, min_stop = x.raw_stop, mind
            hold = (x.session + DAY_MS - entry_ts) // MIN_MS
            f = simulate(
                path,
                entry_ts,
                x.direction,
                stop,
                min_stop,
                "zone",
                x.target,
                int(hold),
                Costs(0, 0, 0),
            )
            if not f.valid:
                skips[f"{g.key} {f.reason}"] = skips.get(f"{g.key} {f.reason}", 0) + 1
                continue
            tdist = x.direction * (x.target - f.entry)
            if tdist <= 0:
                skips[f"{g.key} target_passed"] = skips.get(f"{g.key} target_passed", 0) + 1
                continue
            if g.rule == "H2" and tdist < g.skip_r * f.r_unit:
                skips[f"{g.key} skip_ratio"] = skips.get(f"{g.key} skip_ratio", 0) + 1
                continue
            busy_until = f.exit_ts
            base = {
                "symbol": symbol, "config": g.key, "rule": g.rule, "k_atr": g.k_atr,
                "trend": g.trend, "skip_r": g.skip_r, "session": x.session, "edge": x.edge,
                "direction": x.direction, "decision_ts": x.decision_ts, "entry_ts": entry_ts,
                "entry": f.entry, "stop": f.stop, "r_unit": f.r_unit, "target": x.target,
                "target_r": tdist / f.r_unit, "valid": True,
            }  # fmt: skip
            rows.append({**base, "exit": "target", "exit_ts": f.exit_ts, "exit_px": f.exit_px,
                         "exit_kind": f.exit_kind, "gross_r": f.gross_r, "mfe_r": f.mfe_r,
                         "mae_r": f.mae_r})  # fmt: skip
            if g.rule == "H2" and x.direction * (x.poc - f.entry) > 0:
                fp = simulate(
                    path,
                    entry_ts,
                    x.direction,
                    f.stop,
                    0.0,
                    "zone",
                    x.poc,
                    int(hold),
                    Costs(0, 0, 0),
                )
                rows.append({**base, "exit": "poc", "exit_ts": fp.exit_ts, "exit_px": fp.exit_px,
                             "exit_kind": fp.exit_kind, "gross_r": fp.gross_r,
                             "mfe_r": fp.mfe_r, "mae_r": fp.mae_r})  # fmt: skip
    return rows, skips
