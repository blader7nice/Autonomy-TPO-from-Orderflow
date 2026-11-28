"""Read stored data, print levels / footprint for a timestamp, and sanity plots."""

from __future__ import annotations

import datetime as dt
import random
from pathlib import Path
from typing import TYPE_CHECKING

import polars as pl

from flowlevels.config import AppConfig, LevelsConfig
from flowlevels.data.storage import ParquetStore, utc_date
from flowlevels.footprint.bars import (
    aggregate_footprint,
    aggregate_ohlcv,
    imbalances,
    ladder,
    res_ms,
    summarize_bar,
)
from flowlevels.levels.engine import LevelSet, closed_rows, compute_levels, history_start_ms
from flowlevels.levels.vwap import anchored_vwap

if TYPE_CHECKING:
    from flowlevels.data.quality import Exclusions


def parse_ts(s: str) -> int:
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.UTC)
    return int(d.timestamp() * 1000)


def fmt_ts(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC).strftime("%Y-%m-%d %H:%M UTC")


def load_range(
    store: ParquetStore, symbol: str, start_ms: int, end_ms: int
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """1m OHLCV + footprint for minutes in [start_ms, end_ms] from available days."""
    start, end = utc_date(start_ms), utc_date(end_ms)
    oh, fp = [], []
    for day in store.days("ohlcv_1m", symbol):
        if start <= day <= end:
            oh.append(store.read("ohlcv_1m", symbol, day, day))
            fp.append(store.read("footprint_1m", symbol, day, day))
    if not oh:
        raise FileNotFoundError(f"No stored 1m data for {symbol} between {start} and {end}")
    ohlcv = pl.concat(oh).unique("minute_ts", keep="first").sort("minute_ts")
    foot = pl.concat(fp).unique(["minute_ts", "bucket"], keep="first").sort("minute_ts", "bucket")
    rng = (pl.col("minute_ts") >= start_ms) & (pl.col("minute_ts") <= end_ms)
    return ohlcv.filter(rng), foot.filter(rng)


def levels_at(store: ParquetStore, cfg: AppConfig, symbol: str, t_ms: int) -> LevelSet:
    oh, fp = load_range(store, symbol, history_start_ms(t_ms, cfg.levels), t_ms)
    return compute_levels(symbol, oh, fp, t_ms, cfg.symbol(symbol), cfg.levels)


def render_levels(ls: LevelSet) -> str:
    out = [f"{ls.symbol} levels as of {fmt_ts(ls.t_ms)}  ATR(1h)={ls.atr_1h or float('nan'):.2f}"]
    out.append("")
    out += [f"  {lv.price:>12.2f}  {lv.name}" for lv in sorted(ls.levels, key=lambda x: -x.price)]
    out += ["", "Confluence zones (score, low..high, label):"]
    for z in sorted(ls.zones, key=lambda z: -z.price):
        out.append(f"  {z.score:4.1f}  {z.low:>12.2f} .. {z.high:<12.2f} {z.label}  [{z.zone_id}]")
    return "\n".join(out)


def render_footprint(store: ParquetStore, cfg: AppConfig, symbol: str, t_ms: int, res: str) -> str:
    step = res_ms(res)
    bar_ts = t_ms // step * step
    oh, fp = load_range(store, symbol, bar_ts, bar_ts + step - 1)
    bars = aggregate_footprint(fp, res).filter(pl.col("bar_ts") == bar_ts)
    if bars.height == 0:
        return f"No footprint for {symbol} {res} bar at {fmt_ts(bar_ts)}"
    sc, fc = cfg.symbol(symbol), cfg.footprint
    lad = ladder(bars, bar_ts)
    s = summarize_bar(lad, fc.imb_ratio, fc.imb_min_volume, fc.stack_n)
    buy, sell = imbalances(lad, fc.imb_ratio, fc.imb_min_volume)
    o = aggregate_ohlcv(oh, res).row(0, named=True)
    complete = o["n_minutes"] == step // 60_000
    out = [
        f"{symbol} {res} bar {fmt_ts(bar_ts)}{'' if complete else '  (INCOMPLETE)'}",
        f"O {o['open']:.2f} H {o['high']:.2f} L {o['low']:.2f} C {o['close']:.2f}  "
        f"vol {o['volume']:.3f}  delta {o['delta']:+.3f}  POC {s.poc_bucket * sc.bucket:.2f}",
        f"stacked buy {[(a * sc.bucket, b * sc.bucket) for a, b in s.stacked_buy]}  "
        f"stacked sell {[(a * sc.bucket, b * sc.bucket) for a, b in s.stacked_sell]}",
        "",
        f"{'price':>12}  {'bid (sell aggr)':>16} x {'ask (buy aggr)':<16} {'delta':>10}",
    ]
    for i in range(len(lad.buckets) - 1, -1, -1):
        mark = "  <POC" if int(lad.buckets[i]) == s.poc_bucket else ""
        bi = "*" if sell[i] else " "
        ai = "*" if buy[i] else " "
        out.append(
            f"{lad.buckets[i] * sc.bucket:>12.2f}  {bi}{lad.bid[i]:>15.3f} x "
            f"{lad.ask[i]:<15.3f}{ai}"
            f" {lad.ask[i] - lad.bid[i]:>+10.3f}{mark}"
        )
    out.append("(* = diagonal imbalance)")
    return "\n".join(out)


def sanity_plot(
    store: ParquetStore, cfg: AppConfig, symbol: str, day: dt.date, out_dir: Path
) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    start = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC).timestamp() * 1000)
    end = start + 86_400_000
    ls = levels_at(store, cfg, symbol, start)  # prior-session levels as of session open
    oh, _ = load_range(store, symbol, history_start_ms(end, cfg.levels), end)
    session = oh.filter((pl.col("minute_ts") >= start) & (pl.col("minute_ts") < end))
    bars = aggregate_ohlcv(session, "15m")
    fig, ax = plt.subplots(figsize=(14, 7), dpi=110)
    x = [(t - start) / 3_600_000 for t in bars["bar_ts"].to_list()]
    for xi, o, h, lo, c in zip(
        x, bars["open"], bars["high"], bars["low"], bars["close"], strict=True
    ):
        col = "#2a9d8f" if c >= o else "#e76f51"
        ax.vlines(xi, lo, h, color=col, lw=0.8)
        ax.vlines(xi, min(o, c), max(o, c), color=col, lw=3)
    # Developing daily VWAP and bands, computed with closed data only at each bar close.
    vw: dict[str, list[float]] = {k: [] for k in ("", "+1s", "-1s", "+2s", "-2s")}
    for t in bars["bar_ts"].to_list():
        b = anchored_vwap(closed_rows(oh, t + 900_000), "d", t).bands()
        for k in vw:
            vw[k].append(b.get(k, float("nan")))
    ax.plot(x, vw[""], color="#264653", lw=1.2, label="VWAP d")
    for k, ls_ in (("+1s", "--"), ("-1s", "--"), ("+2s", ":"), ("-2s", ":")):
        ax.plot(x, vw[k], color="#264653", lw=0.8, ls=ls_)
    lo_y, hi_y = float(bars["low"].min()), float(bars["high"].max())  # type: ignore[arg-type]
    pad = (hi_y - lo_y) * 0.15
    for lv in ls.levels:
        if lv.name.startswith("VWAP") or not (lo_y - pad <= lv.price <= hi_y + pad):
            continue
        ax.axhline(lv.price, color="#8d99ae", lw=0.6)
        ax.text(24.1, lv.price, lv.name, fontsize=7, va="center")
    for z in ls.zones:
        if lo_y - pad <= z.price <= hi_y + pad and len(z.members) > 1:
            ax.axhspan(z.low, z.high, color="#e9c46a", alpha=0.25)
    # Data holes recorded by the day pipeline (D-026): no trades in the archive there.
    info = store.completion("ohlcv_1m", symbol, day) or {}
    for i, h in enumerate(info.get("holes", [])):
        ax.axvspan(
            max(h["after_ms"] - start, 0) / 3_600_000,
            min(h["before_ms"] - start, 86_400_000) / 3_600_000,
            color="#adb5bd",
            alpha=0.5,
            label="data hole (no aggTrades in archive)" if i == 0 else None,
        )
    ax.set_xlim(0, 26.5)
    ax.set_ylim(lo_y - pad, hi_y + pad)
    ax.set_xlabel("hour (UTC)")
    ax.set_title(
        f"{symbol} {day} — 15m, prior-session levels, confluence zones, daily VWAP ±1σ/±2σ"
    )
    ax.legend(loc="upper left")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{symbol}-{day}.png"
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def pick_days(
    store: ParquetStore,
    symbol: str,
    n: int,
    seed: int = 42,
    lc: LevelsConfig | None = None,
    excl: Exclusions | None = None,
) -> list[dt.date]:
    """n random complete (archive-built) days that have a full prior day stored.

    Days without a completion marker (e.g. today's partial live day) are never picked. With
    `lc`, sessions whose level inputs overlap a data hole are excluded first (D-031) and
    recorded in `excl`.
    """
    from flowlevels.data.quality import clean_sessions
    from flowlevels.levels.stats import eligible_days

    eligible = eligible_days(store, symbol)
    if lc is not None:
        eligible = clean_sessions(store, symbol, eligible, lc, excl)
    rnd = random.Random(seed)
    return sorted(rnd.sample(eligible, min(n, len(eligible))))
