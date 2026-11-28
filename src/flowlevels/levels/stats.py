"""Level quality statistics over stored real data (D-029): levels and zones per day, widths.

For every eligible session (complete archive day, prior day stored, level inputs not
touching a data hole) levels are computed at the 24 hourly snapshots 00:00 … 23:00 UTC with
closed data only (the same `compute_levels` as everything else).

Per session:
- levels: number of levels at 00:00 (the prior-session set; developing levels are included),
  and the HVN/LVN count among them;
- zones / confluence zones: mean per snapshot of all zones and of zones with >= 2 members;
- confluence zones in range: mean per snapshot of zones with >= 2 members that overlap the
  session's full high-low range (the zones price could actually touch that day; this uses
  the whole session, so it is a report statistic, never a signal input);
- widths: high - low of every confluence zone at every snapshot, in USD and in ATR(1h) at
  that snapshot.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import polars as pl

from flowlevels.config import AppConfig, config_hash, git_commit
from flowlevels.data.quality import Exclusions, clean_sessions
from flowlevels.data.storage import ParquetStore, day_start_ms
from flowlevels.levels.engine import DAY_MS, HOUR_MS, compute_levels, history_start_ms
from flowlevels.levels.report import load_range


@dataclass
class SessionStats:
    symbol: str
    day: dt.date
    n_levels: int
    n_nodes: int
    zones_mean: float
    conf_mean: float
    conf_in_range: float
    widths_usd: list[float]
    widths_atr: list[float]


def eligible_days(store: ParquetStore, symbol: str) -> list[dt.date]:
    days = store.days("ohlcv_1m", symbol)
    done = [d for d in days if store.completion("ohlcv_1m", symbol, d) is not None]
    return [d for d in done if d - dt.timedelta(days=1) in days]


def session_stats(store: ParquetStore, cfg: AppConfig, symbol: str, day: dt.date) -> SessionStats:
    sc, lc = cfg.symbol(symbol), cfg.levels
    d0 = day_start_ms(day)
    oh, fp = load_range(store, symbol, history_start_ms(d0, lc), d0 + DAY_MS - 1)
    n_levels = n_nodes = 0
    zones_n, conf_n = [], []
    in_range: list[int] = []
    sess = oh.filter(pl.col("minute_ts") >= d0)
    s_lo, s_hi = float(sess["low"].min()), float(sess["high"].max())  # type: ignore[arg-type]
    w_usd: list[float] = []
    w_atr: list[float] = []
    for h in range(24):
        t = d0 + h * HOUR_MS
        ls = compute_levels(symbol, oh, fp, t, sc, lc)
        if h == 0:
            n_levels = len(ls.levels)
            n_nodes = sum(lv.type in ("HVN d", "LVN d") for lv in ls.levels)
        conf = [z for z in ls.zones if len(z.members) > 1]
        zones_n.append(len(ls.zones))
        conf_n.append(len(conf))
        in_range.append(sum(z.low <= s_hi and z.high >= s_lo for z in conf))
        for z in conf:
            w_usd.append(z.high - z.low)
            if ls.atr_1h:
                w_atr.append((z.high - z.low) / ls.atr_1h)
    return SessionStats(
        symbol,
        day,
        n_levels,
        n_nodes,
        float(np.mean(zones_n)),
        float(np.mean(conf_n)),
        float(np.mean(in_range)),
        w_usd,
        w_atr,
    )


def collect(
    store: ParquetStore, cfg: AppConfig, symbols: list[str]
) -> tuple[list[SessionStats], Exclusions]:
    excl = Exclusions(holes=[])
    out = []
    for sym in symbols:
        for day in clean_sessions(store, sym, eligible_days(store, sym), cfg.levels, excl):
            out.append(session_stats(store, cfg, sym, day))
    return out, excl


def summarize(stats: list[SessionStats]) -> pl.DataFrame:
    rows = []
    for sym in sorted({s.symbol for s in stats}):
        ss = [s for s in stats if s.symbol == sym]
        wu = np.array([w for s in ss for w in s.widths_usd])
        wa = np.array([w for s in ss for w in s.widths_atr])
        rows.append(
            {
                "symbol": sym,
                "sessions": len(ss),
                "levels/day": float(np.mean([s.n_levels for s in ss])),
                "HVN+LVN/day": float(np.mean([s.n_nodes for s in ss])),
                "zones/snapshot": float(np.mean([s.zones_mean for s in ss])),
                "conf zones/snapshot": float(np.mean([s.conf_mean for s in ss])),
                "conf zones in session range": float(np.mean([s.conf_in_range for s in ss])),
                "width mean USD": float(wu.mean()) if len(wu) else float("nan"),
                "width max USD": float(wu.max()) if len(wu) else float("nan"),
                "width mean ATR": float(wa.mean()) if len(wa) else float("nan"),
                "width max ATR": float(wa.max()) if len(wa) else float("nan"),
            }
        )
    return pl.DataFrame(rows)


def render(stats: list[SessionStats], excl: Exclusions, cfg: AppConfig, title: str) -> str:
    df = summarize(stats)
    cols = df.columns
    lines = [
        f"# {title}",
        "",
        f"Generated {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M} UTC, commit `{git_commit()}`, "
        f"config v{cfg.version} hash `{config_hash(cfg)}`.",
        "",
        excl.summary(),
        "",
        "Definitions: `flowlevels.levels.stats` docstring. Confluence zone = zone with >= 2 "
        "members; widths are high − low incl. tolerance, over all 24 hourly snapshots.",
        "",
        "| " + " | ".join(cols) + " |",
        "|" + "---|" * len(cols),
    ]
    for r in df.iter_rows():
        lines.append(
            "| " + " | ".join(f"{v:.2f}" if isinstance(v, float) else str(v) for v in r) + " |"
        )
    lines += ["", "## Per session", ""]
    lines.append(
        "| symbol | day | levels | HVN+LVN | zones/snap | conf/snap | conf in range | "
        "max width USD | max width ATR |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for s in stats:
        mu = max(s.widths_usd) if s.widths_usd else float("nan")
        ma = max(s.widths_atr) if s.widths_atr else float("nan")
        lines.append(
            f"| {s.symbol} | {s.day} | {s.n_levels} | {s.n_nodes} | {s.zones_mean:.1f} | "
            f"{s.conf_mean:.1f} | {s.conf_in_range:.1f} | {mu:.1f} | {ma:.2f} |"
        )
    return "\n".join(lines) + "\n"
