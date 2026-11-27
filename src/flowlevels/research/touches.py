"""Zone touches at a signal resolution, one zone per touch (D-034, D-035).

Per bar b (range [low, high], previous bar close c):
- candidate zones: zones whose [low, high] intersects the bar range. Static zones are the
  session's fixed zones; dynamic zones are each dynamic level as of the bar open +- the
  session tolerance (zone_tolerance_k_atr x ATR(1h) at the open).
- touch price p*: the bar extreme of its larger excursion from c (low if c - low >=
  high - c, else high), i.e. where price went in that bar.
- the bar is assigned to exactly one zone: the candidate whose **anchor** (static: the
  anchoring member's price; dynamic: the level) is nearest to p*; ties -> higher score,
  then lower anchor price.
- a **touch** starts at a bar whose assigned zone differs from the previous bar's (none or
  another zone). Direction = reversal: long if c is above the zone (came from above), short
  if below; if c is inside the zone, long when c >= the anchor.
- touch_n counts touches per zone id and session (1st, 2nd, 3rd, ...).

Static and dynamic pools are independent: a bar can start one static and one dynamic touch.
Everything uses the bar itself and earlier data only; zone geometry is known at the bar open.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

from flowlevels.levels.engine import Zone


@dataclass(frozen=True)
class SessionZones:
    day0: int
    atr_open: float
    zones: list[Zone]


def _assign(
    lo: np.ndarray,
    hi: np.ndarray,
    prev_c: np.ndarray,
    z_lo: np.ndarray,
    z_hi: np.ndarray,
    anchor: np.ndarray,
    score: np.ndarray,
) -> np.ndarray:
    """Assigned zone index per bar (-1 = none). z_* are (n_bars, n_zones) or (n_zones,)."""
    n = len(lo)
    if z_lo.ndim == 1:
        z_lo, z_hi, anchor = (np.broadcast_to(a, (n, len(a))) for a in (z_lo, z_hi, anchor))
    hit = (z_lo <= hi[:, None]) & (z_hi >= lo[:, None]) & np.isfinite(anchor)
    up = hi - prev_c
    down = prev_c - lo
    p = np.where(down >= up, lo, hi)
    dist = np.where(hit, np.abs(anchor - p[:, None]), np.inf)
    # tie-break: higher score, then lower anchor price (lexicographic via small offsets)
    order = np.lexsort(
        (np.nan_to_num(anchor, nan=np.inf), -np.broadcast_to(score, dist.shape), dist), axis=1
    )
    best = order[:, 0]
    ok = np.isfinite(dist[np.arange(n), best])
    return np.where(ok, best, -1)


def _touch_rows(
    bars: pl.DataFrame,
    idx: np.ndarray,
    prev_c: np.ndarray,
    z_lo: np.ndarray,
    z_hi: np.ndarray,
    anchor: np.ndarray,
    assigned: np.ndarray,
    prev_assigned: int,
) -> list[tuple[int, int, int]]:
    """(bar position, zone index, direction +1/-1) for every touch start."""
    out = []
    last = prev_assigned
    for k in range(len(assigned)):
        a = int(assigned[k])
        if a >= 0 and a != last:
            zl = z_lo[k, a] if z_lo.ndim == 2 else z_lo[a]
            zh = z_hi[k, a] if z_hi.ndim == 2 else z_hi[a]
            an = anchor[k, a] if anchor.ndim == 2 else anchor[a]
            c = prev_c[k]
            d = 1 if c > zh else -1 if c < zl else (1 if c >= an else -1)
            out.append((int(idx[k]), a, d))
        last = a
    return out


def static_touches(
    bars: pl.DataFrame, sessions: dict[int, SessionZones], symbol: str, res: str
) -> pl.DataFrame:
    """bars: sorted bar features of all loaded days (needs bar_ts, high, low, close)."""
    ts = bars["bar_ts"].to_numpy()
    lo, hi, cl = (bars[c].to_numpy() for c in ("low", "high", "close"))
    prev_c = np.r_[np.nan, cl[:-1]]
    day = ts // 86_400_000 * 86_400_000
    rows = []
    for day0, sz in sorted(sessions.items()):
        if not sz.zones:
            continue
        idx = np.flatnonzero((day == day0) & np.isfinite(prev_c))
        if len(idx) == 0:
            continue
        z_lo = np.array([z.low for z in sz.zones])
        z_hi = np.array([z.high for z in sz.zones])
        anc = np.array([next(m.price for m in z.members if m.name == z.anchor) for z in sz.zones])
        sc = np.array([z.score for z in sz.zones])
        a = _assign(lo[idx], hi[idx], prev_c[idx], z_lo, z_hi, anc, sc)
        counts: dict[str, int] = {}
        for pos, zi, d in _touch_rows(bars, idx, prev_c[idx], z_lo, z_hi, anc, a, -1):
            z = sz.zones[zi]
            counts[z.zone_id] = counts.get(z.zone_id, 0) + 1
            anchor_type = next(m.type for m in z.members if m.name == z.anchor)
            rows.append(
                {
                    "symbol": symbol,
                    "res": res,
                    "pool": "static",
                    "session": day0,
                    "pos": pos,
                    "bar_ts": int(ts[pos]),
                    "zone_id": z.zone_id,
                    "zone_low": z.low,
                    "zone_high": z.high,
                    "anchor_price": float(anc[zi]),
                    "anchor_type": anchor_type,
                    "label": z.label,
                    "n_members": len(z.members),
                    "touch_n": counts[z.zone_id],
                    "direction": d,
                    "atr_open": sz.atr_open,
                }
            )
    return pl.DataFrame(rows, schema=TOUCH_SCHEMA)


def dynamic_touches(
    bars: pl.DataFrame,
    dyn_at_bar: pl.DataFrame,
    names: list[str],
    sessions: dict[int, SessionZones],
    tol_k_atr: float,
    symbol: str,
    res: str,
) -> pl.DataFrame:
    """dyn_at_bar: one row per bar (same order as bars) with the dynamic levels at bar open."""
    ts = bars["bar_ts"].to_numpy()
    lo, hi, cl = (bars[c].to_numpy() for c in ("low", "high", "close"))
    prev_c = np.r_[np.nan, cl[:-1]]
    lv = dyn_at_bar.select(names).to_numpy().astype(np.float64)  # NaN = level not defined
    day = ts // 86_400_000 * 86_400_000
    rows = []
    for day0, sz in sorted(sessions.items()):
        idx = np.flatnonzero((day == day0) & np.isfinite(prev_c))
        if len(idx) == 0:
            continue
        tol = tol_k_atr * sz.atr_open
        anc = lv[idx]
        z_lo, z_hi = anc - tol, anc + tol
        a = _assign(lo[idx], hi[idx], prev_c[idx], z_lo, z_hi, anc, np.ones(len(names)))
        counts: dict[str, int] = {}
        sess = _iso(day0)
        for pos, zi, d in _touch_rows(bars, idx, prev_c[idx], z_lo, z_hi, anc, a, -1):
            k = int(np.searchsorted(idx, pos))
            zid = f"{symbol}:{sess}:dyn:{names[zi]}"
            counts[zid] = counts.get(zid, 0) + 1
            rows.append(
                {
                    "symbol": symbol,
                    "res": res,
                    "pool": "dynamic",
                    "session": day0,
                    "pos": pos,
                    "bar_ts": int(ts[pos]),
                    "zone_id": zid,
                    "zone_low": float(z_lo[k, zi]),
                    "zone_high": float(z_hi[k, zi]),
                    "anchor_price": float(anc[k, zi]),
                    "anchor_type": names[zi],
                    "label": names[zi],
                    "n_members": 1,
                    "touch_n": counts[zid],
                    "direction": d,
                    "atr_open": sz.atr_open,
                }
            )
    return pl.DataFrame(rows, schema=TOUCH_SCHEMA)


def _iso(day0: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(day0 / 1000, tz=dt.UTC).date().isoformat()


def dynamic_confluence(
    touches: pl.DataFrame, dyn_at_bar: pl.DataFrame, names: list[str]
) -> pl.DataFrame:
    """Add `dyn_confluence`: dynamic levels (at the touch bar open) inside the static zone."""
    lv = dyn_at_bar.select(names).to_numpy().astype(np.float64)
    pos = touches["pos"].to_numpy()
    zl, zh = touches["zone_low"].to_numpy(), touches["zone_high"].to_numpy()
    out = []
    for i in range(touches.height):
        row = lv[pos[i]]
        out.append(
            [n for n, p in zip(names, row, strict=True) if np.isfinite(p) and zl[i] <= p <= zh[i]]
        )
    return touches.with_columns(pl.Series("dyn_confluence", out, dtype=pl.List(pl.String)))


TOUCH_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String(),
    "res": pl.String(),
    "pool": pl.String(),
    "session": pl.Int64(),
    "pos": pl.Int64(),  # row index of the touch bar in `bars`
    "bar_ts": pl.Int64(),
    "zone_id": pl.String(),
    "zone_low": pl.Float64(),
    "zone_high": pl.Float64(),
    "anchor_price": pl.Float64(),
    "anchor_type": pl.String(),
    "label": pl.String(),
    "n_members": pl.Int64(),
    "touch_n": pl.Int64(),
    "direction": pl.Int64(),  # +1 long (reversal up), -1 short
    "atr_open": pl.Float64(),
}
