"""ITERATION-4 (FINAL) runner, report and example-trade charts.

Pre-registration: docs/research/ITERATION-4-PREREG.md. Windows/costs/kill helpers are the
MASTER-PLAN ones (`research.plan`). Candles from the kline store; c2 orderflow from the 1m
footprint store, built month by month into 30m detector flags (bounded memory).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from flowlevels.config import AppConfig
from flowlevels.data.quality import bar_mask, load_holes
from flowlevels.data.storage import ParquetStore
from flowlevels.levels.report import load_range
from flowlevels.research.plan import SYMBOLS, by_window, load_window, survives, windows
from flowlevels.research.report import STATS_HEAD, _f, _stats_row, apply_costs, uplift_of
from flowlevels.research.stats import GroupStats, mde
from flowlevels.signals.detectors import detector_flags
from flowlevels.signals.features import add_baselines, bar_features
from flowlevels.strategies import method4 as m4

DAY_MS = 86_400_000
PRIOR_CONFIGS = 61


# --------------------------------------------------------------------------- data


def _month_edges(start: int, end: int) -> list[tuple[int, int]]:
    out = []
    d = dt.datetime.fromtimestamp(start / 1000, dt.UTC).replace(day=1, hour=0)
    while int(d.timestamp() * 1000) < end:
        nxt = (d + dt.timedelta(days=32)).replace(day=1)
        a = max(start, int(d.timestamp() * 1000))
        b = min(end, int(nxt.timestamp() * 1000))
        out.append((a, b))
        d = nxt
    return out


def orderflow_for(
    store: ParquetStore, cfg: AppConfig, symbol: str, start: int, end: int
) -> tuple[m4.OrderflowFlags | None, dict[str, Any]]:
    """30m detector flags for [start - warm-up, end), built month by month."""
    i4, fc = cfg.iteration4, cfg.footprint
    res = i4.orderflow_res
    f0 = start - i4.footprint_warmup_days * DAY_MS
    parts = []
    for a, b in _month_edges(f0, end):
        try:
            oh, fp = load_range(store, symbol, a, b - 1)
        except FileNotFoundError:
            continue
        if oh.height:
            parts.append(bar_features(oh, fp, res, fc))
        del oh, fp
    if not parts:
        return None, {"bars": 0}
    bars = pl.concat(parts).sort("bar_ts")
    holes = load_holes(store, symbol)
    step = m4.TF_MS[res]
    in_hole = (
        bar_mask(bars["bar_ts"], step, holes)
        if holes
        else pl.Series("in_hole", [False] * bars.height)
    )
    bars = add_baselines(bars, fc, excluded=in_hole if holes else None)
    bars = detector_flags(bars, cfg.signals).with_columns(in_hole.alias("in_hole"))
    days = bars["bar_ts"] // DAY_MS
    info = {
        "bars": bars.height,
        "days": int(days.n_unique()),
        "first": str(dt.datetime.fromtimestamp(int(bars["bar_ts"][0]) / 1000, dt.UTC).date()),
        "bars_in_hole": int(in_hole.sum()),
        **{
            f"{d}_{s}": int(bars[f"{d}_{s}"].sum())
            for d in i4.orderflow_detectors
            for s in ("long", "short")
        },
    }
    return m4.orderflow_flags(bars, i4.orderflow_detectors), info


def iter4_trades(
    cfg: AppConfig, kroot: Path, store: ParquetStore
) -> tuple[pl.DataFrame, dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    for w, (start, end) in windows(cfg).items():
        for s in SYMBOLS:
            m = load_window(kroot, s, cfg, w)
            of, info = orderflow_for(store, cfg, s, start, end)
            r, mt = m4.run_symbol(m, of, start, end, cfg, s)
            mt["orderflow"] = info
            for x in r:
                x["window"] = w
            rows += r
            meta[f"{w} {s}"] = mt
    df = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    return df, meta


# --------------------------------------------------------------------------- report


def session_se(df: pl.DataFrame, scen: str, reps: int, seed: int) -> float:
    """Session-clustered bootstrap SE of the mean net R."""
    if df.height < 2:
        return float("nan")
    g = df.group_by("symbol", "session").agg(
        pl.col(f"net_r_{scen}").sum().alias("s"), pl.len().alias("n")
    )
    s, n = g["s"].to_numpy(), g["n"].to_numpy().astype(float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(s), size=(reps, len(s)))
    return float((s[idx].sum(1) / n[idx].sum(1)).std(ddof=1))


def _grid_table(
    tr: pl.DataFrame, cfg: AppConfig
) -> tuple[list[str], list[tuple[m4.Grid, dict[str, GroupStats]]]]:
    L = [
        "| config | N A | exp A | N B | exp B | N A+B | exp A+B R | bps | win | PF | max DD R | eligible |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    out = []
    for g in m4.grids(cfg):
        st = by_window(tr.filter(pl.col("config") == g.key))
        out.append((g, st))
        ab = st["A+B"]
        el = "yes" if ab.n >= cfg.plan.min_trades else f"n<{cfg.plan.min_trades}"
        L.append(
            f"| {g.key} | {st['A'].n} | {_f(st['A'].exp_r)} | {st['B'].n} | {_f(st['B'].exp_r)} | "
            f"{ab.n} | {_f(ab.exp_r)} | {ab.exp_bps:+.1f} | {ab.win_rate:.1%} | {ab.pf:.2f} | "
            f"{ab.max_dd_r:.1f} | {el} |"
            if ab.n
            else f"| {g.key} | 0 | | 0 | | 0 | | | | | | {el} |"
        )
    return L, out


def _sens(x: pl.DataFrame, cfg: AppConfig) -> list[str]:
    L = ["| scenario | A | B | A+B | A+B bps |", "|---|---|---|---|---|"]
    for sc in cfg.plan.sensitivities:
        st = by_window(x, sc)
        L.append(
            f"| {sc} | {_f(st['A'].exp_r)} | {_f(st['B'].exp_r)} | {_f(st['A+B'].exp_r)} | "
            f"{st['A+B'].exp_bps:+.1f} |"
        )
    return L


def _monthly(x: pl.DataFrame) -> tuple[list[str], float]:
    mo = (
        x.with_columns(pl.from_epoch("entry_ts", time_unit="ms").dt.strftime("%Y-%m").alias("m"))
        .group_by("m")
        .agg(
            pl.len().alias("n"),
            pl.col("net_r_default").sum().alias("sum"),
            pl.col("net_r_default").mean().alias("mean"),
        )
        .sort("m")
    )
    L = ["| month | N | sum R | mean R |", "|---|---|---|---|"]
    L += [f"| {a} | {n} | {s:+.2f} | {mu:+.3f} |" for a, n, s, mu in mo.iter_rows()]
    share = float(np.mean(mo["sum"].to_numpy() > 0)) if mo.height else float("nan")
    return L, share


def report(
    tr_raw: pl.DataFrame, meta: dict[str, Any], cfg: AppConfig
) -> tuple[str, dict[str, Any], str | None]:
    n_cfg = len(m4.grids(cfg))
    L = [f"## ITERATION-4 (FINAL) — TrejdingHub method replica ({n_cfg} configurations)", ""]
    L += ["### Funnel (all configurations share it)", ""]
    L.append(
        "| window symbol | POIs by kind | setups | c1 | c2 | c2 lacking footprint | 30m bars (in hole) | absorption L/S | exhaustion L/S |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|")
    for k, mt in meta.items():
        o = mt["orderflow"]
        L.append(
            f"| {k} | {', '.join(f'{a} {b}' for a, b in sorted(mt['pois'].items()))} | "
            f"{mt['setups']} | {mt['c1']} | {mt['c2']} | {mt['c2_no_footprint']} | "
            f"{o.get('bars', 0)} ({o.get('bars_in_hole', 0)}) | "
            f"{o.get('absorption_long', 0)}/{o.get('absorption_short', 0)} | "
            f"{o.get('exhaustion_long', 0)}/{o.get('exhaustion_short', 0)} |"
        )
    L.append("")
    L.append("Setups are counted before the window filter (warm-up setups are dropped as "
             "`outside_window`). c1/c2 count the setups passing the confirmation.")  # fmt: skip
    L.append("")
    out: dict[str, Any] = {"n_configs": n_cfg, "cumulative_configs": PRIOR_CONFIGS + n_cfg}
    if tr_raw.height == 0:
        L.append("**No trades in any configuration → KILLED; FINAL-KILL stands.**")
        out.update(verdict="KILLED", reason="no trades")
        return "\n".join(L) + "\n", out, None
    tr = apply_costs(tr_raw, cfg, only=tuple(cfg.plan.sensitivities))
    L += ["### Grid (default costs; primary and only exit: structural target / stop / 24 h)", ""]
    gl, grid_rows = _grid_table(tr, cfg)
    L += gl + [""]
    L.append("Skips per configuration (A+B, both symbols): " + "; ".join(
        f"`{g.key}`: " + ", ".join(
            f"{r} {sum(mt['skips'][g.key].get(r, 0) for mt in meta.values())}"
            for r in sorted({r for mt in meta.values() for r in mt['skips'][g.key]})
        ) for g, _ in grid_rows
    ))  # fmt: skip
    L.append("")
    cand = [(g, st) for g, st in grid_rows if st["A+B"].n >= cfg.plan.min_trades]
    sel_key: str | None
    if not cand:
        g, st = max(grid_rows, key=lambda c: c[1]["A+B"].n)
        sel_key = g.key
        why = f"no configuration with ≥ {cfg.plan.min_trades} trades on A+B"
        L.append(f"**Verdict: KILLED — {why}. FINAL-KILL stands.** (Details below use the "
                 f"configuration with the most trades, `{g.key}`, for information only.)")  # fmt: skip
        out.update(verdict="KILLED", reason=why, selected=None)
    else:
        g, st = sorted(cand, key=lambda c: (-c[1]["A+B"].exp_r, c[1]["A+B"].max_dd_r))[0]
        sel_key = g.key
        ok, why = survives(st)
        verdict = "PASSES" if ok else "KILLED"
        L.append(f"**Selected `{g.key}` → {verdict}** — {why}." + (
            "" if ok else " **FINAL-KILL stands.**"))  # fmt: skip
        out.update(verdict=verdict, reason=why, selected=g.key)
    out["stats"] = {w: s.__dict__ for w, s in st.items()}
    L.append("")
    x = tr.filter(pl.col("config") == sel_key)
    L += [f"### Configuration `{sel_key}`", "", STATS_HEAD]
    for w, s in by_window(x).items():
        L.append(_stats_row(w, s))
    L += ["", "Cost sensitivity (expectancy R; identical trades):", ""] + _sens(x, cfg) + [""]
    L += ["Breakdown (A+B, default costs):", "", STATS_HEAD]
    for name, flt in (
        ("BTCUSDT", pl.col("symbol") == "BTCUSDT"), ("ETHUSDT", pl.col("symbol") == "ETHUSDT"),
        ("long", pl.col("direction") == 1), ("short", pl.col("direction") == -1),
    ):  # fmt: skip
        L.append(_stats_row(name, by_window(x.filter(flt))["A+B"]))
    L.append("")
    kinds = dict(x.group_by("exit_kind").len().iter_rows())
    L.append(
        f"Exits {kinds}; median stop {float((x['r_unit'] / x['entry'] * 1e4).median()):.0f} bps; "  # type: ignore[arg-type]
        f"median target {float(x['target_r'].median()):.2f} R; closed at a window end: "  # type: ignore[arg-type]
        f"{int(x['window_end_close'].sum())}."
    )
    poi_kinds = (
        x.select(pl.col("pois").str.split(";").list.eval(pl.element().str.split(":").list.first()))
        .explode("pois")["pois"].value_counts().sort("count", descending=True)
    )  # fmt: skip
    L.append(
        "POI kinds behind the trades: " + ", ".join(f"{a} {b}" for a, b in poi_kinds.iter_rows())
    )
    L.append("")
    ml, share = _monthly(x)
    L += [f"Monthly stability: {share:.0%} of months with net R > 0.", ""] + ml + [""]
    rc = cfg.research
    se = session_se(x, "default", rc.bootstrap_reps, rc.seed)
    n_sess = 2 * sum((e - s) // DAY_MS for s, e in windows(cfg).values())
    n_hold = 2 * ((cfg.plan.holdout_end - cfg.plan.holdout_start).days + 1)
    md = mde(se, n_sess, n_hold, rc.ci_level, rc.power) if np.isfinite(se) else float("nan")
    out["mde_r"] = md
    L.append(
        f"MDE for the hold-out (net expectancy > 0, one-sided α {(1 - rc.ci_level) / 2:.2f}, power "
        f"{rc.power:.0%}): session-clustered SE {se:.3f} R over {n_sess} symbol-sessions (A+B), "
        f"scaled to {n_hold} hold-out symbol-sessions → **MDE {md:.3f} R**."
    )
    L.append("")
    # uplift c2 vs c1
    L += ["### Uplift c2 vs c1 (same bias and minRR; default costs, A+B)", ""]
    L.append(
        "| bias, minRR | N c1 | exp c1 | N c2 | exp c2 | c2 − c1 | N c1 not confirmed | exp | c2 vs not confirmed [90 % CI] |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|")
    c2_keys = {(s, int(t), int(d)) for mt in meta.values() for (s, t, d) in mt["c2_keys"]}
    for b in cfg.iteration4.bias:
        for r in cfg.iteration4.min_rr:
            c1 = tr.filter(pl.col("config") == m4.Grid(b, "c1", r).key)
            c2 = tr.filter(pl.col("config") == m4.Grid(b, "c2", r).key)
            keys = list(zip(c1["symbol"], c1["closeback_ts"], c1["direction"], strict=True))
            mask = [(s, int(t), int(d)) not in c2_keys for s, t, d in keys]
            un = c1.filter(pl.Series(mask, dtype=pl.Boolean))
            e1, e2 = by_window(c1)["A+B"], by_window(c2)["A+B"]
            eu = by_window(un)["A+B"]
            up = uplift_of(c2, un, cfg) if c2.height and un.height else None
            ci = f"{_f(up.uplift)} [{_f(up.lo)}, {_f(up.hi)}]" if up else "–"
            L.append(
                f"| {b}, {r:g} | {e1.n} | {_f(e1.exp_r)} | {e2.n} | {_f(e2.exp_r)} | "
                f"{_f(e2.exp_r - e1.exp_r) if e1.n and e2.n else '–'} | {eu.n} | {_f(eu.exp_r)} | {ci} |"
            )
    L.append("")
    L.append(f"**Cumulative configurations tried in the in-sample windows: {PRIOR_CONFIGS} + "
             f"{n_cfg} = {PRIOR_CONFIGS + n_cfg}.** Hold-out runs: 0.")  # fmt: skip
    return "\n".join(L) + "\n", out, sel_key


# --------------------------------------------------------------------------- charts

INK, INK2, GRID = "#191919", "#666666", "#e6e6e6"
C_ENTRY, C_STOP, C_TGT, C_POI, C_SWING, C_EMA, C_HI = (
    "#257de0", "#ec4647", "#008a17", "#f47230", "#6a4ce2", "#00b894", "#f2a600",
)  # fmt: skip


def chart_trade(t: dict[str, Any], m: pl.DataFrame, cfg: AppConfig, path: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    i4 = cfg.iteration4
    a = int(t["sweep_ts"]) - 30 * m4.H1
    b = max(int(t["exit_ts"]), int(t["entry_ts"])) + 3 * m4.H1
    b15 = m4.make_bars(m.filter((pl.col("minute_ts") >= a) & (pl.col("minute_ts") < b)), m4.M15)
    b4 = m4.make_bars(m.filter(pl.col("minute_ts") < b), m4.H4)
    fig, ax = plt.subplots(figsize=(11, 5.2), dpi=110)
    x = (b15.ts - b15.ts[0]) / m4.M15
    for xi, o, h, lo, c in zip(x, b15.o, b15.h, b15.l, b15.c, strict=True):
        up = c >= o
        ax.vlines(xi, lo, h, color=INK2, lw=0.7, zorder=2)
        ax.bar(xi, abs(c - o) or (h - lo) * 0.02, bottom=min(o, c), width=0.7,
               color="white" if up else INK2, edgecolor=INK2, lw=0.7, zorder=3)  # fmt: skip

    def xp(ts: int) -> float:
        return float((ts - b15.ts[0]) / m4.M15)

    marks = [(int(t["sweep_ts"]), "sweep"), (int(t["closeback_ts"]), "close-back")]
    if marks[0][0] == marks[1][0]:
        marks = [(marks[0][0], "sweep + close-back")]
    else:  # adjacent bars: one label, both bars shaded
        marks = [(marks[0][0], "sweep, then close-back"), (marks[1][0], "")]
    for ts_k, lab in marks:
        ax.axvspan(xp(ts_k) - 0.45, xp(ts_k) + 0.45, color=C_HI, alpha=0.35, zorder=1)
        if lab:
            ax.annotate(lab, (xp(ts_k), float(t["extreme"])), xytext=(-8, -14 if t["direction"] > 0 else 8),
                        textcoords="offset points", ha="right", fontsize=8, color=INK)  # fmt: skip
    x_end = float(x[-1]) + 0.5
    xe = xp(int(t["entry_ts"]))
    for y, col, lab, ls in (
        (t["entry"], C_ENTRY, "entry", "-"), (t["stop"], C_STOP, "stop", "-"),
        (t["target"], C_TGT, "target", "-"), (t["swing"], C_SWING, "15m swing", ":"),
    ):  # fmt: skip
        x0 = xe if lab != "15m swing" else 0
        ax.hlines(y, x0, x_end, colors=col, lw=1.6 if ls == "-" else 1.2, linestyles=ls, zorder=4)  # type: ignore[arg-type]
        ax.text(x_end + 0.3, y, f"{lab} {y:,.2f}", va="center", fontsize=8, color=INK)
    poi_txt: list[str] = []
    for p in str(t["pois"]).split(";"):
        kind, rest = p.split(":")
        near, far = (float(v) for v in rest.split("/"))
        if near == far:
            ax.hlines(near, 0, x_end, colors=C_POI, lw=1.2, linestyles="--", zorder=4)
        else:
            ax.axhspan(min(near, far), max(near, far), color=C_POI, alpha=0.15, zorder=0)
        poi_txt.append(f"{kind} {near:,.2f}" + ("" if near == far else f"–{far:,.2f}"))
    ax.text(0.01, 0.02, "POI (orange): " + "; ".join(poi_txt), transform=ax.transAxes, fontsize=8,
            color=INK, va="bottom", bbox={"facecolor": "white", "alpha": 0.8, "lw": 0})  # fmt: skip
    if t["bias"] == "b1":
        ema = pl.Series(b4.c).ewm_mean(span=i4.ema_period, adjust=False).to_numpy()
        ct = b4.ts + m4.H4  # EMA value is known at the 4H close
        sel = (ct >= b15.ts[0]) & (ct <= b15.ts[-1] + m4.M15)
        ax.step([xp(int(v)) for v in ct[sel]], ema[sel], where="post", color=C_EMA, lw=1.4,
                label="4H EMA50 (at 4H close)", zorder=4)  # fmt: skip
    else:
        hi, lw = m4.fractals(b4.h, b4.l, i4.fractal_n)
        for j in np.flatnonzero(hi | lw):
            tj = int(b4.ts[j])
            if b15.ts[0] <= tj <= b15.ts[-1]:
                yv = b4.h[j] if hi[j] else b4.l[j]
                ax.plot(xp(tj) + 7.5, yv, marker="v" if hi[j] else "^", ms=8, color=C_EMA, zorder=5)
    xx = xp(int(t["exit_ts"]) // m4.M15 * m4.M15)
    ax.plot(xx, t["exit_px"], marker="X", ms=10, color=INK, zorder=6)
    ax.annotate(f"exit ({t['exit_kind']})", (xx, t["exit_px"]), xytext=(9, 6), ha="left",
                textcoords="offset points", fontsize=8, color=INK)  # fmt: skip
    ax.axvline(xe - 0.5, color=C_ENTRY, lw=0.8, alpha=0.6)
    ticks = np.arange(0, len(x), 16)
    ax.set_xticks(ticks)
    ax.set_xticklabels(
        [dt.datetime.fromtimestamp(b15.ts[i] / 1000, dt.UTC).strftime("%m-%d %H:%M") for i in ticks],
        fontsize=8, color=INK2,
    )  # fmt: skip
    ax.tick_params(axis="y", labelsize=8, colors=INK2)
    ax.grid(True, color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.set_xlim(-1, x_end + 9)
    side = "long" if t["direction"] > 0 else "short"
    ax.set_title(f"{title}\n4H bias {t['bias']}: {side} at the decision", fontsize=10, color=INK,
                 loc="left")  # fmt: skip
    if t["bias"] == "b1":
        ax.legend(loc="upper left", fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def example_trades(
    tr_raw: pl.DataFrame, sel_key: str, cfg: AppConfig, kroot: Path, out_dir: Path
) -> str:
    tr = apply_costs(tr_raw.filter(pl.col("config") == sel_key), cfg, only=("default",))
    tr = tr.sort("net_r_default")
    n = tr.height
    if n == 0:
        return "No trades to show.\n"
    picks: list[tuple[str, int]] = []
    for i in range(min(3, n)):
        picks.append(("best", n - 1 - i))
    for i in range(min(3, n)):
        picks.append(("worst", i))
    mid = n // 2
    for i in (mid - 2, mid - 1, mid, mid + 1):
        if 0 <= i < n:
            picks.append(("median", i))
    seen: set[int] = set()
    picks = [(k, i) for k, i in picks if not (i in seen or seen.add(i))]  # type: ignore[func-returns-value]
    out_dir.mkdir(parents=True, exist_ok=True)
    cache: dict[tuple[str, str], pl.DataFrame] = {}
    L = []
    for n_ex, (kind, i) in enumerate(picks, 1):
        t = tr.row(i, named=True)
        key = (t["window"], t["symbol"])
        if key not in cache:
            cache[key] = load_window(kroot, t["symbol"], cfg, t["window"])
        ts = dt.datetime.fromtimestamp(t["entry_ts"] / 1000, dt.UTC)
        side = "LONG" if t["direction"] > 0 else "SHORT"
        title = (f"#{n_ex} {kind}: {t['symbol']} {side} {ts:%Y-%m-%d %H:%M} UTC, window {t['window']}, "
                 f"bias {t['bias']}, {t['conf']}, net {t['net_r_default']:+.2f} R")  # fmt: skip
        fn = f"trade-{n_ex:02d}-{kind}.png"
        chart_trade(t, cache[key], cfg, out_dir / fn, title)
        L += [
            f"### {n_ex}. {kind} — {t['symbol']} {side}, entry {ts:%Y-%m-%d %H:%M} UTC (window {t['window']})",
            "",
            f"![trade {n_ex}]({out_dir.name}/{fn})",
            "",
            f"- POI(s): `{t['pois']}`; 15m swing {t['swing']:,.2f}; sweep bar "
            f"{dt.datetime.fromtimestamp(t['sweep_ts'] / 1000, dt.UTC):%m-%d %H:%M}, close-back bar "
            f"{dt.datetime.fromtimestamp(t['closeback_ts'] / 1000, dt.UTC):%m-%d %H:%M}, sweep extreme {t['extreme']:,.2f}",
            f"- entry {t['entry']:,.2f}, stop {t['stop']:,.2f} ({t['r_unit'] / t['entry'] * 1e4:.0f} bps), "
            f"target {t['target']:,.2f} ({t['target_r']:.2f} R); exit {t['exit_kind']} at {t['exit_px']:,.2f} "
            f"{dt.datetime.fromtimestamp(t['exit_ts'] / 1000, dt.UTC):%m-%d %H:%M}; gross {t['gross_r']:+.2f} R, "
            f"net {t['net_r_default']:+.2f} R ({t['bps_default']:+.1f} bps)",
            "",
        ]
    return "\n".join(L) + "\n"
