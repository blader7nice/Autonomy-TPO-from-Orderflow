"""Dynamic levels as per-minute series, for bar-by-bar research (D-034).

Row for minute m holds each dynamic level as of time t = m (the start of minute m), i.e.
from 1m rows that closed by m only (minute_ts < m). This is the value a bar starting at m
sees at its open, so a touch of that bar uses no data from the bar itself.

- VWAP d/w/m and +-1/2 sigma bands: exclusive running sums per anchor period; null until
  the anchor has `min_minutes` closed minutes (bands of a few minutes are meaningless).
- dPOC: developing TPO POC of the UTC session, incrementally, with the exact conventions
  of `levels.profile` (period high/low marks every row in between, POC ties -> closest to
  the middle row, then lower). A test checks both against `levels.engine.dynamic_levels`.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from flowlevels.config import LevelsConfig, SymbolConfig
from flowlevels.levels.profile import price_to_row

DAY_MS = 86_400_000
MINUTE_MS = 60_000

VWAP_NAMES = [
    f"VWAP {k}{' ' + s if s else ''}"
    for k in ("d", "w", "m")
    for s in ("", "+1s", "-1s", "+2s", "-2s")
]
DYNAMIC_NAMES = ["dPOC", *VWAP_NAMES]


def _anchor_expr(kind: str) -> pl.Expr:
    t = pl.col("minute_ts")
    if kind == "d":
        return t // DAY_MS * DAY_MS
    if kind == "w":
        day = t // DAY_MS
        return (day - (day + 3) % 7) * DAY_MS  # 1970-01-01 was a Thursday (weekday 3)
    if kind == "m":
        return (
            pl.from_epoch(t, time_unit="ms")
            .dt.truncate("1mo")
            .dt.epoch(time_unit="ms")
            .cast(pl.Int64)
        )
    raise ValueError(kind)


def vwap_series(oh_1m: pl.DataFrame, min_minutes: int) -> pl.DataFrame:
    x = oh_1m.sort("minute_ts").select("minute_ts", "volume", "sum_pv", "sum_p2v")
    cols = []
    for kind in ("d", "w", "m"):
        a = f"_a{kind}"
        x = x.with_columns(_anchor_expr(kind).alias(a))
        g = pl.col(a)
        v = pl.col("volume").cum_sum().over(g) - pl.col("volume")
        pv = pl.col("sum_pv").cum_sum().over(g) - pl.col("sum_pv")
        p2v = pl.col("sum_p2v").cum_sum().over(g) - pl.col("sum_p2v")
        n = pl.int_range(pl.len()).over(g)  # closed minutes of this anchor before m
        x = x.with_columns(v.alias("_v"), pv.alias("_pv"), p2v.alias("_p2v"), n.alias("_n"))
        ok = (pl.col("_v") > 0) & (pl.col("_n") >= min_minutes)
        vw = pl.col("_pv") / pl.col("_v")
        sig = (pl.col("_p2v") / pl.col("_v") - vw * vw).clip(lower_bound=0.0).sqrt()
        x = x.with_columns(
            pl.when(ok).then(vw).alias(f"VWAP {kind}"), pl.when(ok).then(sig).alias("_sig")
        ).with_columns(
            (pl.col(f"VWAP {kind}") + m * pl.col("_sig")).alias(f"VWAP {kind} {lab}")
            for m, lab in ((1, "+1s"), (-1, "-1s"), (2, "+2s"), (-2, "-2s"))
        )
        cols += [f"VWAP {kind}"] + [f"VWAP {kind} {lab}" for lab in ("+1s", "-1s", "+2s", "-2s")]
    return x.select("minute_ts", *cols)


def _poc(counts: np.ndarray) -> int:
    mx = counts.max()
    cand = np.flatnonzero(counts == mx)
    mid = (len(counts) - 1) / 2.0
    return int(min(cand, key=lambda i: (abs(i - mid), i)))


def dpoc_series(oh_1m: pl.DataFrame, sc: SymbolConfig, lc: LevelsConfig) -> pl.DataFrame:
    x = oh_1m.sort("minute_ts")
    ts = x["minute_ts"].to_numpy()
    lo = price_to_row(x["low"].to_numpy(), sc.tpo_tick, sc.exchange_tick)
    hi = price_to_row(x["high"].to_numpy(), sc.tpo_tick, sc.exchange_tick)
    per_ms = lc.tpo_period_minutes * MINUTE_MS
    out = np.full(len(ts), np.nan)
    day = ts // DAY_MS
    starts = np.flatnonzero(np.r_[True, day[1:] != day[:-1]])
    ends = np.r_[starts[1:], len(ts)]
    for s, e in zip(starts, ends, strict=True):
        base = int(lo[s:e].min())
        counts = np.zeros(int(hi[s:e].max()) - base + 1)  # completed periods
        r_lo = r_hi = None  # profile row span so far (rows of completed + current period)
        cur_p, plo, phi = None, 0, 0
        for i in range(s, e):
            # state as of ts[i]: minutes s..i-1 are closed
            if i > s:
                prof = counts.copy()
                prof[plo - base : phi - base + 1] += 1.0
                assert r_lo is not None and r_hi is not None
                out[i] = (r_lo + _poc(prof[r_lo - base : r_hi - base + 1]) + 0.5) * sc.tpo_tick
            p = ts[i] // per_ms
            if p != cur_p:
                if cur_p is not None:
                    counts[plo - base : phi - base + 1] += 1.0
                cur_p, plo, phi = p, int(lo[i]), int(hi[i])
            else:
                plo, phi = min(plo, int(lo[i])), max(phi, int(hi[i]))
            r_lo = plo if r_lo is None else min(r_lo, plo)
            r_hi = phi if r_hi is None else max(r_hi, phi)
    return pl.DataFrame({"minute_ts": ts, "dPOC": out}).with_columns(pl.col("dPOC").fill_nan(None))


def dynamic_series(
    oh_1m: pl.DataFrame, sc: SymbolConfig, lc: LevelsConfig, min_minutes: int
) -> pl.DataFrame:
    """minute_ts + one column per dynamic level (DYNAMIC_NAMES), values as of minute start."""
    v = vwap_series(oh_1m, min_minutes)
    n_day = pl.int_range(pl.len()).over(pl.col("minute_ts") // DAY_MS)
    d = dpoc_series(oh_1m, sc, lc).with_columns(
        pl.when(n_day >= min_minutes).then(pl.col("dPOC")).alias("dPOC")
    )
    return d.join(v, on="minute_ts").select("minute_ts", *DYNAMIC_NAMES).sort("minute_ts")
