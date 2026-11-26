"""Levels engine: all key levels and confluence zones as of time t.

No lookahead: only 1m rows that are CLOSED at t (minute_ts + 60s <= t) are used. Prior-
session levels use complete prior sessions only; developing levels (dPOC, VWAPs, naked-POC
status) use closed rows of the current session.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field

import numpy as np
import polars as pl

from flowlevels.config import LevelsConfig, SymbolConfig
from flowlevels.footprint.minute import MINUTE_MS
from flowlevels.levels.profile import hvn_lvn, tpo_profile, value_area, volume_profile
from flowlevels.levels.vwap import anchor_ms, anchored_vwap

DAY_MS = 86_400_000
HOUR_MS = 3_600_000


@dataclass(frozen=True, slots=True)
class Level:
    name: str  # display label, e.g. "VAL d", "nPOC 2026-01-03"
    type: str  # weight key, e.g. "VAL d", "nPOC"
    price: float


@dataclass(frozen=True, slots=True)
class Zone:
    zone_id: str
    label: str
    score: float
    price: float
    low: float
    high: float
    members: tuple[Level, ...]
    anchor: str = ""  # name of the anchoring (strongest) member


@dataclass(frozen=True)
class LevelSet:
    symbol: str
    t_ms: int
    atr_1h: float | None
    levels: list[Level] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)

    def level(self, name: str) -> float | None:
        for lv in self.levels:
            if lv.name == name:
                return lv.price
        return None


def _f(x: object) -> float:
    assert isinstance(x, (int, float))
    return float(x)


def closed_rows(df: pl.DataFrame, t_ms: int) -> pl.DataFrame:
    return df.filter(pl.col("minute_ts") + MINUTE_MS <= t_ms)


def _between(df: pl.DataFrame, start: int, end: int) -> pl.DataFrame:
    return df.filter((pl.col("minute_ts") >= start) & (pl.col("minute_ts") < end))


def atr_1h(ohlcv_closed: pl.DataFrame, t_ms: int, period: int) -> float | None:
    """SMA of true range over the last `period` complete 1h bars before t."""
    hours = (
        ohlcv_closed.filter(pl.col("minute_ts") < t_ms // HOUR_MS * HOUR_MS)
        .with_columns((pl.col("minute_ts") // HOUR_MS).alias("h"))
        .group_by("h")
        .agg(pl.col("high").max(), pl.col("low").min(), pl.col("close").last())
        .sort("h")
        .tail(period + 1)
    )
    if hours.height < period + 1:
        return None
    h, lo, c = (hours[k].to_numpy() for k in ("high", "low", "close"))
    tr = np.maximum(h[1:] - lo[1:], np.maximum(abs(h[1:] - c[:-1]), abs(lo[1:] - c[:-1])))
    return float(tr.mean())


def _profile_levels(
    ohlcv: pl.DataFrame,
    fp: pl.DataFrame,
    sc: SymbolConfig,
    lc: LevelsConfig,
    suffix: str,
    tpo: bool,
    node_atr: float | None,
) -> list[Level]:
    """Profile levels of one session. HVN/LVN only when node_atr is given (the minimum node
    distance is node_atr * hvn_min_distance_k_atr)."""
    out: list[Level] = []
    if tpo:
        va = value_area(
            tpo_profile(ohlcv, lc.tpo_period_minutes, sc.tpo_tick, sc.exchange_tick),
            lc.value_area_pct,
        )
        if va:
            out += [
                Level(f"{n} {suffix}", f"{n} {suffix}", p)
                for n, p in (("POC", va.poc), ("VAH", va.vah), ("VAL", va.val))
            ]
    vp = volume_profile(fp, sc.bucket, sc.tpo_tick, sc.exchange_tick)
    vva = value_area(vp, lc.value_area_pct)
    if vva:
        out += [
            Level(f"{n} {suffix}", f"{n} {suffix}", p)
            for n, p in (("vPOC", vva.poc), ("vVAH", vva.vah), ("vVAL", vva.val))
        ]
    if node_atr is not None:
        hv, lv = hvn_lvn(
            vp,
            lc.hvn_smoothing_rows,
            lc.hvn_min_prominence,
            lc.hvn_min_distance_k_atr * node_atr,
            lc.hvn_top_n,
            lc.lvn_top_n,
        )
        out += [Level(f"HVN {suffix}", f"HVN {suffix}", p) for p in hv]
        out += [Level(f"LVN {suffix}", f"LVN {suffix}", p) for p in lv]
    return out


def compute_levels(
    symbol: str,
    ohlcv_1m: pl.DataFrame,
    fp_1m: pl.DataFrame,
    t_ms: int,
    sc: SymbolConfig,
    lc: LevelsConfig,
) -> LevelSet:
    oh = closed_rows(ohlcv_1m, t_ms)
    fp = closed_rows(fp_1m, t_ms)
    day0 = anchor_ms("d", t_ms)
    week0 = anchor_ms("w", t_ms)
    levels: list[Level] = []

    # Prior day: TPO + VP + nodes + range. Node spacing uses ATR(1h) as of the session open,
    # so prior-day nodes are constant within the session (no nodes without that ATR).
    pd_oh, pd_fp = _between(oh, day0 - DAY_MS, day0), _between(fp, day0 - DAY_MS, day0)
    node_atr = atr_1h(oh, day0, lc.atr_period_1h)
    levels += _profile_levels(pd_oh, pd_fp, sc, lc, "d", tpo=True, node_atr=node_atr)
    if pd_oh.height:
        levels += [
            Level("PDH", "PDH", _f(pd_oh["high"].max())),
            Level("PDL", "PDL", _f(pd_oh["low"].min())),
        ]

    # Prior week: VP + range.
    pw_oh, pw_fp = _between(oh, week0 - 7 * DAY_MS, week0), _between(fp, week0 - 7 * DAY_MS, week0)
    levels += _profile_levels(pw_oh, pw_fp, sc, lc, "w", tpo=False, node_atr=None)
    if pw_oh.height:
        levels += [
            Level("PWH", "PWH", _f(pw_oh["high"].max())),
            Level("PWL", "PWL", _f(pw_oh["low"].min())),
        ]

    # Developing TPO POC of the current session.
    today = oh.filter(pl.col("minute_ts") >= day0)
    dva = value_area(
        tpo_profile(today, lc.tpo_period_minutes, sc.tpo_tick, sc.exchange_tick),
        lc.value_area_pct,
    )
    if dva:
        levels.append(Level("dPOC", "dPOC", dva.poc))

    # Naked TPO POCs from sessions day-2 .. day-lookback, untouched since their session end.
    for k in range(2, lc.naked_poc_lookback_days + 1):
        start = day0 - k * DAY_MS
        sess = _between(oh, start, start + DAY_MS)
        va = value_area(
            tpo_profile(sess, lc.tpo_period_minutes, sc.tpo_tick, sc.exchange_tick),
            lc.value_area_pct,
        )
        if va is None:
            continue
        after = oh.filter(pl.col("minute_ts") >= start + DAY_MS)
        touched = after.filter((pl.col("low") <= va.poc) & (pl.col("high") >= va.poc)).height
        if not touched:
            d = dt.datetime.fromtimestamp(start / 1000, tz=dt.UTC).date().isoformat()
            levels.append(Level(f"nPOC {d}", "nPOC", va.poc))

    # Anchored VWAPs with bands.
    for kind in ("d", "w", "m"):
        acc = anchored_vwap(oh, kind, t_ms)
        for suf, price in acc.bands().items():
            name = f"VWAP {kind}{' ' + suf if suf else ''}"
            levels.append(Level(name, name, price))

    atr = atr_1h(oh, t_ms, lc.atr_period_1h)
    zones = confluence(symbol, levels, atr, sc, lc) if atr else []
    return LevelSet(symbol, t_ms, atr, levels, zones)


def confluence(
    symbol: str, levels: list[Level], atr: float, sc: SymbolConfig, lc: LevelsConfig
) -> list[Zone]:
    """Anchored clustering with a hard width cap (D-029). It cannot chain.

    Repeat until every level is assigned:
    1. Anchor = the strongest unassigned level: highest weight; ties -> highest summed weight
       of unassigned levels within confluence_k_atr * ATR (densest spot); then lower price,
       then name.
    2. Candidates = unassigned levels within confluence_k_atr * ATR of the anchor, taken
       closest-first (ties: higher weight, lower price, name). A candidate joins only if the
       member span stays <= zone_max_width_k_atr * ATR - 2 * tolerance.
    Zone edges = [min member - tol, max member + tol], so width <= zone_max_width_k_atr * ATR.
    Every member is within the radius of the anchor, so there is no chaining.
    Zones are returned sorted by price.
    """
    if not levels:
        return []
    radius = lc.confluence_k_atr * atr
    tol = lc.zone_tolerance_k_atr * atr
    max_span = lc.zone_max_width_k_atr * atr - 2 * tol
    eps = 1e-9 * max(atr, 1.0)
    pool = sorted(levels, key=lambda lv: (lv.price, lv.name))
    zones = []
    while pool:
        dens = {
            id(a): sum(lc.weight(x.type) for x in pool if abs(x.price - a.price) <= radius + eps)
            for a in pool
        }
        anchor = min(pool, key=lambda a: (-lc.weight(a.type), -dens[id(a)], a.price, a.name))
        cands = sorted(
            (x for x in pool if x is not anchor and abs(x.price - anchor.price) <= radius + eps),
            key=lambda x: (abs(x.price - anchor.price), -lc.weight(x.type), x.price, x.name),
        )
        members = [anchor]
        lo = hi = anchor.price
        for x in cands:
            nlo, nhi = min(lo, x.price), max(hi, x.price)
            if nhi - nlo <= max_span + eps:
                members.append(x)
                lo, hi = nlo, nhi
        taken = {id(m) for m in members}
        pool = [x for x in pool if id(x) not in taken]
        c = sorted(members, key=lambda lv: (lv.price, lv.name))
        w = np.array([lc.weight(lv.type) for lv in c])
        p = np.array([lv.price for lv in c])
        price = float((w * p).sum() / w.sum()) if w.sum() > 0 else float(p.mean())
        label = " | ".join(lv.name for lv in c)
        key = f"{symbol}|{label}|{round(price / sc.exchange_tick)}"
        zones.append(
            Zone(
                zone_id=hashlib.sha1(key.encode()).hexdigest()[:12],
                label=label,
                score=float(w.sum()),
                price=price,
                low=float(p.min()) - tol,
                high=float(p.max()) + tol,
                members=tuple(c),
                anchor=anchor.name,
            )
        )
    return sorted(zones, key=lambda z: (z.price, z.label))


def history_start_ms(t_ms: int, lc: LevelsConfig) -> int:
    """Earliest minute any level at t may depend on (to load just enough data)."""
    day0 = anchor_ms("d", t_ms)
    return min(
        anchor_ms("m", t_ms),
        anchor_ms("w", t_ms) - 7 * DAY_MS,
        day0 - lc.naked_poc_lookback_days * DAY_MS,
        t_ms // HOUR_MS * HOUR_MS - (lc.atr_period_1h + 2) * HOUR_MS,
    )
