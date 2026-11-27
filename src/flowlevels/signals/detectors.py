"""Signal detectors (SPEC 8), vectorized over the bars of one resolution.

Each detector is a pure function of bar features up to and including the bar being
evaluated (known at its close); `lookback` features use earlier bars only. They return a
flag per bar and per reversal direction: `long` = bullish reversal (price came down into a
zone), `short` = bearish. The mirrored definitions use the opposite side.

Long (bullish) definitions; short mirrors them:
1. absorption: heavy volume (volume_z >= vol_z_min) with sellers aggressive
   (-delta / volume >= delta_frac_min) but the bar closes in its upper part
   (close_pos >= close_pos_min): selling was absorbed.
2. exhaustion: the bar makes a new low against the last `lookback_bars` bars, the volume at
   its lowest bucket is <= extreme_frac_max x its POC volume (sellers ran out), the ladder
   has >= min_buckets buckets and it closes at close_pos >= close_pos_min.
3. delta_divergence: a new low against the last `lookback_bars` bars with positive delta.
4. stacked_imbalance: a stacked BUY imbalance in the lower half of the ladder, delta > 0.
5. trapped: the previous bar had strong selling (delta_z <= -delta_z_min) and closed in its
   lower part (close_pos <= close_pos_max); this bar closes above the previous bar's high.
6. liquidation spike: informational only (forceOrder is live-only and incomplete, SPEC 4.4);
   it never enters the score or the research statistics, so it is not computed here.

The confirmation score is the weighted count of distinct detectors that fired.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from flowlevels.config import SignalsConfig

DETECTORS = ("absorption", "exhaustion", "delta_divergence", "stacked_imbalance", "trapped")
DIRECTIONS = ("long", "short")


@dataclass(frozen=True, slots=True)
class Signal:
    type: str
    direction: str  # long | short
    strength: float  # [0, 1]
    ts: int  # bar close (ms): when the signal is known
    zone_id: str = ""
    features: dict[str, float] = field(default_factory=dict)


def detector_flags(bars: pl.DataFrame, sc: SignalsConfig) -> pl.DataFrame:
    """bars (features + volume_z, delta_z) -> bars + one boolean column per detector and
    direction, e.g. `absorption_long`, plus `absorption_strength`."""
    a, e, dv, tr = sc.absorption, sc.exhaustion, sc.delta_divergence, sc.trapped
    vol = pl.col("volume")
    dfrac = pl.when(vol > 0).then(pl.col("delta") / vol).otherwise(0.0)
    vz = pl.col("volume_z").fill_null(float("-inf"))
    dz = pl.col("delta_z")

    def prior_min(c: str, n: int) -> pl.Expr:
        return pl.col(c).shift(1).rolling_min(n, min_samples=n)

    def prior_max(c: str, n: int) -> pl.Expr:
        return pl.col(c).shift(1).rolling_max(n, min_samples=n)

    cp = pl.col("close_pos")
    exh_ok = (pl.col("n_buckets") >= e.min_buckets) & (pl.col("poc_vol") > 0)
    out = bars.with_columns(
        # 1 absorption
        ((vz >= a.vol_z_min) & (-dfrac >= a.delta_frac_min) & (cp >= a.close_pos_min)).alias(
            "absorption_long"
        ),
        ((vz >= a.vol_z_min) & (dfrac >= a.delta_frac_min) & (cp <= 1 - a.close_pos_min)).alias(
            "absorption_short"
        ),
        # 2 exhaustion
        (
            exh_ok
            & (pl.col("low") < prior_min("low", e.lookback_bars))
            & (pl.col("low_vol") <= e.extreme_frac_max * pl.col("poc_vol"))
            & (cp >= e.close_pos_min)
        )
        .fill_null(False)
        .alias("exhaustion_long"),
        (
            exh_ok
            & (pl.col("high") > prior_max("high", e.lookback_bars))
            & (pl.col("high_vol") <= e.extreme_frac_max * pl.col("poc_vol"))
            & (cp <= 1 - e.close_pos_min)
        )
        .fill_null(False)
        .alias("exhaustion_short"),
        # 3 delta divergence
        ((pl.col("low") < prior_min("low", dv.lookback_bars)) & (pl.col("delta") > 0))
        .fill_null(False)
        .alias("delta_divergence_long"),
        ((pl.col("high") > prior_max("high", dv.lookback_bars)) & (pl.col("delta") < 0))
        .fill_null(False)
        .alias("delta_divergence_short"),
        # 4 stacked imbalance reversal
        (pl.col("stk_buy_low") & (pl.col("delta") > 0)).alias("stacked_imbalance_long"),
        (pl.col("stk_sell_high") & (pl.col("delta") < 0)).alias("stacked_imbalance_short"),
        # 5 trapped traders
        (
            (dz.shift(1) <= -tr.delta_z_min)
            & (cp.shift(1) <= tr.close_pos_max)
            & (pl.col("close") > pl.col("high").shift(1))
        )
        .fill_null(False)
        .alias("trapped_long"),
        (
            (dz.shift(1) >= tr.delta_z_min)
            & (cp.shift(1) >= 1 - tr.close_pos_max)
            & (pl.col("close") < pl.col("low").shift(1))
        )
        .fill_null(False)
        .alias("trapped_short"),
        (pl.col("volume_z").fill_null(0.0) / (2 * a.vol_z_min))
        .clip(0.0, 1.0)
        .alias("absorption_strength"),
    )
    return out


def signals_at(row: dict[str, object], direction: str, zone_id: str, res_ms: int) -> list[Signal]:
    """The detectors that fired on one flagged bar row, as Signal objects (live / UI use)."""
    out = []
    for d in DETECTORS:
        if row.get(f"{d}_{direction}"):
            strength = float(row["absorption_strength"]) if d == "absorption" else 1.0  # type: ignore[arg-type]
            out.append(
                Signal(
                    type=d,
                    direction=direction,
                    strength=strength,
                    ts=int(row["bar_ts"]) + res_ms,  # type: ignore[call-overload]
                    zone_id=zone_id,
                    features={
                        k: float(row[k])  # type: ignore[arg-type]
                        for k in ("volume_z", "delta_z", "close_pos", "delta")
                        if row.get(k) is not None
                    },
                )
            )
    return out
