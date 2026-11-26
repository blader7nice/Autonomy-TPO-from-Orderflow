"""TPO and volume profiles: POC, value area, HVN/LVN.

Conventions (docs/DECISIONS.md D-016):
- Profile rows are integer indices of size `row_size` (tpo_tick); row r covers
  [r*row_size, (r+1)*row_size). POC price = row midpoint; VAH = upper edge of the top VA
  row; VAL = lower edge of the bottom VA row.
- POC = row with the max count; ties -> closest to the profile's middle row, then lower.
- Value area (70%): start at the POC; repeatedly compare the sum of the next two rows above
  with the next two rows below and add the larger pair (one row if only one remains on that
  side); on a tie add both pairs. Stop when included >= pct * total.
- TPO: each 30-min period contributes 1 to every row between its low and high (inclusive).
- Volume profile: footprint volume per row.
- HVN/LVN (D-028): centered moving average (window hvn_smoothing_rows, edges use the
  available rows); HVN/LVN = strict local maxima/minima of the smoothed profile (plateaus:
  the middle row), ranked by topographic prominence, a minimum distance apart, top N each.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl


@dataclass(frozen=True, slots=True)
class Profile:
    rows: np.ndarray  # int64 contiguous ascending
    counts: np.ndarray  # float64
    row_size: float

    @property
    def empty(self) -> bool:
        return len(self.rows) == 0 or float(self.counts.sum()) <= 0


@dataclass(frozen=True, slots=True)
class ValueArea:
    poc: float
    vah: float
    val: float
    poc_row: int


def _contiguous(rows: np.ndarray, counts: np.ndarray, row_size: float) -> Profile:
    if len(rows) == 0:
        return Profile(np.zeros(0, np.int64), np.zeros(0), row_size)
    lo, hi = int(rows.min()), int(rows.max())
    out = np.zeros(hi - lo + 1, dtype=np.float64)
    np.add.at(out, rows - lo, counts)
    return Profile(np.arange(lo, hi + 1, dtype=np.int64), out, row_size)


def price_to_row(price: np.ndarray | float, row_size: float, exchange_tick: float) -> np.ndarray:
    ticks = np.round(np.asarray(price, dtype=np.float64) / exchange_tick).astype(np.int64)
    per_row = round(row_size / exchange_tick)
    return np.asarray(ticks // per_row)


def tpo_profile(
    ohlcv_1m: pl.DataFrame, period_minutes: int, row_size: float, exchange_tick: float
) -> Profile:
    if ohlcv_1m.height == 0:
        return _contiguous(np.zeros(0, np.int64), np.zeros(0), row_size)
    step = period_minutes * 60_000
    per = (
        ohlcv_1m.with_columns((pl.col("minute_ts") // step).alias("p"))
        .group_by("p")
        .agg(pl.col("high").max(), pl.col("low").min())
    )
    lo_r = price_to_row(per["low"].to_numpy(), row_size, exchange_tick)
    hi_r = price_to_row(per["high"].to_numpy(), row_size, exchange_tick)
    base = int(lo_r.min())
    diff = np.zeros(int(hi_r.max()) - base + 2, dtype=np.float64)
    np.add.at(diff, lo_r - base, 1.0)
    np.add.at(diff, hi_r - base + 1, -1.0)
    counts = np.cumsum(diff)[:-1]
    return Profile(np.arange(base, base + len(counts), dtype=np.int64), counts, row_size)


def volume_profile(
    fp_1m: pl.DataFrame, bucket_size: float, row_size: float, exchange_tick: float
) -> Profile:
    if fp_1m.height == 0:
        return _contiguous(np.zeros(0, np.int64), np.zeros(0), row_size)
    per_bucket = round(bucket_size / exchange_tick)
    per_row = round(row_size / exchange_tick)
    g = fp_1m.group_by("bucket").agg((pl.col("ask_vol") + pl.col("bid_vol")).sum().alias("v"))
    rows = (g["bucket"].to_numpy() * per_bucket) // per_row
    return _contiguous(rows.astype(np.int64), g["v"].to_numpy(), row_size)


def poc_index(counts: np.ndarray) -> int:
    mx = counts.max()
    cand = np.flatnonzero(counts == mx)
    mid = (len(counts) - 1) / 2.0
    return int(min(cand, key=lambda i: (abs(i - mid), i)))


def value_area(p: Profile, pct: float) -> ValueArea | None:
    if p.empty:
        return None
    c = p.counts
    n = len(c)
    poc = poc_index(c)
    a = b = poc
    included = c[poc]
    target = pct * c.sum()
    while included < target and (a > 0 or b < n - 1):
        up_n = min(2, n - 1 - b)
        dn_n = min(2, a)
        up = c[b + 1 : b + 1 + up_n].sum() if up_n else -1.0
        dn = c[a - dn_n : a].sum() if dn_n else -1.0
        if up > dn:
            b += up_n
            included += up
        elif dn > up:
            a -= dn_n
            included += dn
        else:
            b += up_n
            a -= dn_n
            included += up + dn
    rs = p.row_size
    return ValueArea(
        poc=float((p.rows[poc] + 0.5) * rs),
        vah=float((p.rows[b] + 1) * rs),
        val=float(p.rows[a] * rs),
        poc_row=int(p.rows[poc]),
    )


def smooth(counts: np.ndarray, window: int) -> np.ndarray:
    if window <= 1 or len(counts) == 0:
        return counts.astype(np.float64)
    half = window // 2
    cs = np.concatenate([[0.0], np.cumsum(counts, dtype=np.float64)])
    idx = np.arange(len(counts))
    lo = np.maximum(idx - half, 0)
    hi = np.minimum(idx + half + 1, len(counts))
    return np.asarray((cs[hi] - cs[lo]) / (hi - lo))


def _plateau_extrema(s: np.ndarray, sign: int) -> list[int]:
    """Strict local maxima (sign=+1) or minima (sign=-1) of s; plateaus -> middle index.

    Only interior points qualify (both neighbours exist and are strictly lower/higher).
    """
    v = sign * s
    out: list[int] = []
    i, n = 1, len(v)
    while i < n - 1:
        if v[i] > v[i - 1]:
            j = i
            while j + 1 < n and v[j + 1] == v[i]:
                j += 1  # plateau
            if j + 1 < n and v[j + 1] < v[i]:
                out.append((i + j) // 2)
            i = j + 1
        else:
            i += 1
    return out


def prominences(v: np.ndarray, peaks: list[int]) -> np.ndarray:
    """Topographic prominence of each peak of v (same definition as scipy's peak_prominences).

    From the peak, walk left until a strictly higher point or the array edge, and take the
    minimum on the way; the same to the right. Prominence = height - max(left min, right min).
    """
    out = np.zeros(len(peaks), dtype=np.float64)
    n = len(v)
    for k, p in enumerate(peaks):
        h = v[p]
        i, left_min = p, h
        while i > 0 and v[i - 1] <= h:
            i -= 1
            left_min = min(left_min, v[i])
        j, right_min = p, h
        while j < n - 1 and v[j + 1] <= h:
            j += 1
            right_min = min(right_min, v[j])
        out[k] = h - max(left_min, right_min)
    return out


def _select(
    idx: list[int], prom: np.ndarray, rows: np.ndarray, floor: float, min_dist_rows: float, n: int
) -> list[int]:
    """Greedy top-n by prominence (ties: lower row first), >= min_dist_rows apart."""
    order = sorted(range(len(idx)), key=lambda k: (-prom[k], idx[k]))
    chosen: list[int] = []
    for k in order:
        if len(chosen) >= n:
            break
        if prom[k] <= 0 or prom[k] < floor:
            continue
        r = int(rows[idx[k]])
        if all(abs(r - int(rows[c])) >= min_dist_rows for c in chosen):
            chosen.append(idx[k])
    return sorted(chosen)


def hvn_lvn(
    p: Profile,
    window: int,
    min_prominence: float,
    min_distance: float,
    top_hvn: int,
    top_lvn: int,
) -> tuple[list[float], list[float]]:
    """(HVN prices, LVN prices) at row midpoints, ascending (D-028).

    - The volume profile is smoothed with a centered moving average of `window` rows.
    - HVN candidates: strict local maxima of the smoothed profile. Prominence is measured with
      the profile padded by zero volume on both sides (no volume traded beyond the range), so
      a peak at the profile edge is measured against zero.
    - LVN candidates: strict interior local minima. Prominence = depth below the lower of the
      two surrounding highs (prominence of the inverted profile, no padding: the profile
      edges are not LVNs).
    - Keep candidates with prominence >= min_prominence * max(smoothed), then the top N by
      prominence, greedily, skipping a candidate closer than `min_distance` (price units) to
      an already chosen node of the same kind.
    """
    if p.empty or len(p.counts) < 3:
        return [], []
    s = smooth(p.counts, window)
    floor = min_prominence * float(s.max())
    min_rows = min_distance / p.row_size
    padded = np.concatenate([[0.0], s, [0.0]])
    peaks = _plateau_extrema(s, +1)
    hv = _select(
        peaks, prominences(padded, [k + 1 for k in peaks]), p.rows, floor, min_rows, top_hvn
    )
    troughs = _plateau_extrema(s, -1)
    lv = _select(troughs, prominences(-s, troughs), p.rows, floor, min_rows, top_lvn)
    rs = p.row_size
    return (
        [float((p.rows[k] + 0.5) * rs) for k in hv],
        [float((p.rows[k] + 0.5) * rs) for k in lv],
    )
