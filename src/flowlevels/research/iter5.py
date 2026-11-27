"""ITERATION-5 (FINAL, binding hold-out) runner, report and example-trade charts.

Pre-registration: docs/research/ITERATION-5-PREREG.md. Windows and helpers are the MASTER-PLAN
ones (`research.plan`); candles only, from the kline store, with the 150-day warm-up of
`iteration5.warmup_days`. Window A's data ends the day before the hold-out. B's warm-up lies in
the hold-out's calendar span and is indicator input only (owner decision 2026-11-27). Nothing is
simulated or scored outside A or B.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from flowlevels.config import AppConfig
from flowlevels.data import klines
from flowlevels.research.iter4 import _monthly, _sens, session_se
from flowlevels.research.plan import _ms, by_window, survives, windows
from flowlevels.research.report import STATS_HEAD, _f, _stats_row, apply_costs
from flowlevels.research.stats import GroupStats, mde
from flowlevels.strategies import method4 as m4
from flowlevels.strategies import method5 as m5

DAY_MS = 86_400_000
PRIOR_CONFIGS = 69


# --------------------------------------------------------------------------- data


def load_window5(root: Path, symbol: str, cfg: AppConfig, w: str) -> pl.DataFrame:
    """1m klines [window start - warm-up, window end). A ends before the hold-out; B starts
    after it (its warm-up is hold-out-span indicator input, owner decision 2026-11-27)."""
    start, end = windows(cfg)[w]
    hs, he = _ms(cfg.plan.holdout_start), _ms(cfg.plan.holdout_end) + DAY_MS
    if w == "A":
        assert end <= hs, "window A must end before the hold-out"
    if w == "B":
        assert start >= he, "window B must start after the hold-out"
    return klines.load(root, symbol, start - cfg.iteration5.warmup_days * DAY_MS, end)


def iter5_trades(cfg: AppConfig, kroot: Path) -> tuple[pl.DataFrame, dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    for w, (start, end) in windows(cfg).items():
        for s in cfg.iteration5.symbols:
            m = load_window5(kroot, s, cfg, w)
            ts = m["minute_ts"]
            cov = {
                "first": str(dt.datetime.fromtimestamp(int(ts[0]) / 1000, dt.UTC).date()),
                "last_ts": int(ts[-1]),
                "missing_in_window": int((end - start) // 60_000)
                - int(((ts >= start) & (ts < end)).sum()),
                "warmup_minutes": int((ts < start).sum()),
            }
            assert int(ts[-1]) < end
            r, mt = m5.run_symbol(m, start, end, cfg, s)
            mt["coverage"] = cov
            for x in r:
                x["window"] = w
            rows += r
            meta[f"{w} {s}"] = mt
    df = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    return df, meta


# --------------------------------------------------------------------------- report


def gate(
    st: dict[str, GroupStats], per_sym: dict[str, GroupStats], cfg: AppConfig
) -> tuple[bool, str]:
    ok, why = survives(st)
    pos = sorted(s for s, g in per_sym.items() if g.n and g.exp_r > 0)
    need = cfg.iteration5.min_positive_symbols
    sym_ok = len(pos) >= need
    msg = f"{why}; positive symbols {len(pos)}/{len(per_sym)} ({', '.join(pos) or 'none'}), need ≥ {need}"
    return ok and sym_ok, msg


def _per_symbol(x: pl.DataFrame, cfg: AppConfig) -> dict[str, GroupStats]:
    return {s: by_window(x.filter(pl.col("symbol") == s))["A+B"] for s in cfg.iteration5.symbols}


def report(
    tr_raw: pl.DataFrame, meta: dict[str, Any], cfg: AppConfig
) -> tuple[str, dict[str, Any], str | None]:
    i5 = cfg.iteration5
    gl = m5.grids(cfg)
    n_cfg = len(gl)
    syms = i5.symbols
    L = [
        f"## ITERATION-5 (FINAL, binding hold-out) — TrejdingHub method, revised ({n_cfg} configurations)",
        "",
    ]
    L += [
        "**Provenance:** the rules were revised after seeing the ITERATION-4 charts, so these "
        "in-sample numbers are not an unbiased test; the hold-out is the binding verdict.",
        "",
        "### Data and funnel (shared by all configurations)",
        "",
        "| window symbol | data from | missing min in window | POIs by kind | T1 sweeps | no c1 | range < 1.5 ATR15 | T1 setups | T2 touches | T2 engulf / pin | T2 setups |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for wkey, mt in meta.items():
        c = mt["coverage"]
        kinds = mt.get("t2_kinds", {})
        L.append(
            f"| {wkey} | {c['first']} | {c['missing_in_window']} | "
            f"{', '.join(f'{a} {b}' for a, b in sorted(mt['pois'].items()))} | {mt.get('t1_sweeps', 0)} | "
            f"{mt.get('t1_no_c1', 0)} | {mt.get('t1_small_range', 0)} | {mt.get('t1', 0)} | "
            f"{mt.get('t2_touches', 0)} | {kinds.get('engulf', 0)} / {kinds.get('pin', 0)} | {mt.get('t2', 0)} |"
        )
    L += ["", "Setups are counted before the window filter (warm-up setups are dropped as "
          "`outside_window`) and before the bias, daily-limit and TP1 ≥ 1 R checks.", ""]  # fmt: skip
    out: dict[str, Any] = {"n_configs": n_cfg, "cumulative_configs": PRIOR_CONFIGS + n_cfg}
    if tr_raw.height == 0:
        L.append("**No trades in any configuration → KILLED; FINAL-KILL stands.**")
        out.update(verdict="KILLED", reason="no trades", selected=None)
        return "\n".join(L) + "\n", out, None
    tr = apply_costs(tr_raw, cfg, only=tuple(cfg.plan.sensitivities))

    # grid
    L += ["### Grid (default costs, all six symbols)", ""]
    L += [
        "| config | N A | exp A | N B | exp B | N A+B | exp A+B R | bps | win | PF | max DD R | symbols > 0 | eligible |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    rows: list[tuple[m5.Grid, dict[str, GroupStats], dict[str, GroupStats]]] = []
    for g in gl:
        x = tr.filter(pl.col("config") == g.key)
        st, ps = by_window(x), _per_symbol(x, cfg)
        rows.append((g, st, ps))
        ab = st["A+B"]
        npos = sum(1 for v in ps.values() if v.n and v.exp_r > 0)
        el = "yes" if ab.n >= i5.min_trades else f"n<{i5.min_trades}"
        L.append(
            f"| {g.key} | {st['A'].n} | {_f(st['A'].exp_r)} | {st['B'].n} | {_f(st['B'].exp_r)} | "
            f"{ab.n} | {_f(ab.exp_r)} | {ab.exp_bps:+.1f} | {ab.win_rate:.1%} | {ab.pf:.2f} | "
            f"{ab.max_dd_r:.1f} | {npos}/6 | {el} |"
            if ab.n
            else f"| {g.key} | 0 | | 0 | | 0 | | | | | | 0/6 | {el} |"
        )
    L.append("")
    # T1 vs T2
    L += ["### T1 (sweep) vs T2 (zone + confirmation candle), A+B, default costs", ""]
    L += ["| k, second half | T1 N | T1 exp R | T1 A / B | T2 N | T2 exp R | T2 A / B | T2 − T1 |",
          "|---|---|---|---|---|---|---|---|"]  # fmt: skip
    by_key = {g.key: st for g, st, _ in rows}
    for k in i5.stop_k_atr:
        for e in i5.second_exit:
            a = by_key[m5.Grid("T1", k, e).key]
            b = by_key[m5.Grid("T2", k, e).key]
            diff = _f(b["A+B"].exp_r - a["A+B"].exp_r) if a["A+B"].n and b["A+B"].n else "–"
            L.append(
                f"| k {k:g}, {e} | {a['A+B'].n} | {_f(a['A+B'].exp_r)} | {_f(a['A'].exp_r)} / {_f(a['B'].exp_r)} | "
                f"{b['A+B'].n} | {_f(b['A+B'].exp_r)} | {_f(b['A'].exp_r)} / {_f(b['B'].exp_r)} | {diff} |"
            )
    L.append("")
    # per symbol x config
    L += ["### Per symbol and configuration (A+B, default costs: N / net exp R)", ""]
    L += ["| config | " + " | ".join(syms) + " |", "|---|" + "---|" * len(syms)]
    for g, _, ps in rows:
        L.append(
            f"| {g.key} | "
            + " | ".join(f"{ps[s].n} / {_f(ps[s].exp_r)}" if ps[s].n else "0" for s in syms)
            + " |"
        )
    L.append("")
    L.append("Skips per configuration (A+B, all symbols): " + "; ".join(
        f"`{g.key}`: " + ", ".join(
            f"{r} {sum(mt['skips'][g.key].get(r, 0) for mt in meta.values())}"
            for r in sorted({r for mt in meta.values() for r in mt['skips'][g.key]})
        ) for g, _, _ in rows
    ))  # fmt: skip
    L.append("")

    # selection + gate
    cand = [(g, st, ps) for g, st, ps in rows if st["A+B"].n >= i5.min_trades]
    if not cand:
        g, st, ps = max(rows, key=lambda c: c[1]["A+B"].n)
        why = f"no configuration with ≥ {i5.min_trades} trades on A+B"
        L.append(f"**Verdict: KILLED — {why}. FINAL-KILL stands.** (Details below use the "
                 f"configuration with the most trades, `{g.key}`, for information only.)")  # fmt: skip
        out.update(verdict="KILLED", reason=why, selected=None)
    else:
        g, st, ps = sorted(cand, key=lambda c: (-c[1]["A+B"].exp_r, c[1]["A+B"].max_dd_r))[0]
        ok, why = gate(st, ps, cfg)
        verdict = "PASSES the in-sample gate" if ok else "KILLED"
        L.append(f"**Selected `{g.key}` → {verdict}** — {why}." + (
            " STOP: the hold-out runs only after the owner's explicit go." if ok
            else " **FINAL-KILL stands.**"))  # fmt: skip
        out.update(verdict="PASSES" if ok else "KILLED", reason=why, selected=g.key)
    sel_key = g.key
    out["stats"] = {w: s.__dict__ for w, s in st.items()}
    out["per_symbol"] = {s: v.__dict__ for s, v in ps.items()}
    L.append("")

    x = tr.filter(pl.col("config") == sel_key)
    L += [f"### Configuration `{sel_key}`", "", STATS_HEAD]
    for w, gs in by_window(x).items():
        L.append(_stats_row(w, gs))
    L += ["", "Cost sensitivity (expectancy R; identical trades):", ""] + _sens(x, cfg) + [""]
    L += ["Per symbol (A+B; A and B net exp R in the last column):", "",
          STATS_HEAD.split("\n")[0] + " A / B |", STATS_HEAD.split("\n")[1] + "---|"]  # fmt: skip
    for s in syms:
        sw = by_window(x.filter(pl.col("symbol") == s))
        L.append(_stats_row(s, sw["A+B"]) + f" {_f(sw['A'].exp_r)} / {_f(sw['B'].exp_r)} |")
    L += ["", "Direction (A+B):", "", STATS_HEAD]
    for name, flt in (("long", pl.col("direction") == 1), ("short", pl.col("direction") == -1)):
        L.append(_stats_row(name, by_window(x.filter(flt))["A+B"]))
    L.append("")
    kinds = dict(x.group_by("exit_kind").len().sort("exit_kind").iter_rows())
    L.append(
        f"Exits {kinds}; median stop {float((x['r_unit'] / x['entry'] * 1e4).median()):.0f} bps "  # type: ignore[arg-type]
        f"({float((x['r_unit'] / x['atr1h']).median()):.2f} × ATR(1h)); median TP1 "  # type: ignore[arg-type]
        f"{float(x['tp1_r'].median()):.2f} R; closed at a window end: "  # type: ignore[arg-type]
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
    n_sym = len(syms)
    n_sess = n_sym * sum((e - s) // DAY_MS for s, e in windows(cfg).values())
    n_hold = n_sym * ((cfg.plan.holdout_end - cfg.plan.holdout_start).days + 1)
    md = mde(se, n_sess, n_hold, rc.ci_level, rc.power) if np.isfinite(se) else float("nan")
    out["mde_r"] = md
    L.append(
        f"MDE for the hold-out (net expectancy > 0, one-sided α {(1 - rc.ci_level) / 2:.2f}, power "
        f"{rc.power:.0%}): session-clustered SE {se:.3f} R over {n_sess} symbol-sessions (A+B), "
        f"scaled to {n_hold} hold-out symbol-sessions → **MDE {md:.3f} R**."
    )
    L.append("")
    L.append(f"**Cumulative configurations tried in the in-sample windows: {PRIOR_CONFIGS} + "
             f"{n_cfg} = {PRIOR_CONFIGS + n_cfg}.** Hold-out runs: 0.")  # fmt: skip
    return "\n".join(L) + "\n", out, sel_key


# --------------------------------------------------------------------------- charts

INK, INK2, GRID = "#191919", "#666666", "#e6e6e6"
C_ENTRY, C_STOP, C_TGT, C_POI, C_SWING, C_EMA, C_HI, C_DEMA = (
    "#257de0", "#ec4647", "#008a17", "#f47230", "#6a4ce2", "#00b894", "#f2a600", "#c0369d",
)  # fmt: skip


def _utc(ms: int, f: str = "%m-%d %H:%M") -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime(f)


def chart_trade(t: dict[str, Any], m: pl.DataFrame, cfg: AppConfig, path: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    i5 = cfg.iteration5
    step = int(t["trig_step"])
    a = int(t["a_ts"]) - (30 if step == m4.M15 else 48) * m4.H1
    b = int(t["exit_ts"]) + 3 * m4.H1
    bb = m4.make_bars(m.filter((pl.col("minute_ts") >= a) & (pl.col("minute_ts") < b)), step)
    past = m.filter(pl.col("minute_ts") < b)
    b4, bd = m4.make_bars(past, m4.H4), m4.make_bars(past, m4.DAY)
    fig, ax = plt.subplots(figsize=(11.5, 5.6), dpi=110)
    x = (bb.ts - bb.ts[0]) / step
    for xi, o, h, lo, c in zip(x, bb.o, bb.h, bb.l, bb.c, strict=True):
        ax.vlines(xi, lo, h, color=INK2, lw=0.7, zorder=2)
        ax.bar(xi, abs(c - o) or (h - lo) * 0.02, bottom=min(o, c), width=0.7,
               color="white" if c >= o else INK2, edgecolor=INK2, lw=0.7, zorder=3)  # fmt: skip

    def xp(ts: int) -> float:
        return float((ts - bb.ts[0]) / step)

    d = int(t["direction"])
    labs = ("sweep", "close-back") if t["trigger"] == "T1" else ("touch", "confirmation")
    marks = [(int(t["a_ts"]), labs[0]), (int(t["b_ts"]), labs[1])]
    if marks[0][0] == marks[1][0]:
        marks = [(marks[0][0], f"{labs[0]} + {labs[1]}")]
    else:
        marks = [(marks[0][0], f"{labs[0]} … {labs[1]}"), (marks[1][0], "")]
        if t["trigger"] == "T2":  # shade every bar of the window t0 .. j
            marks += [(ts_, "") for ts_ in range(marks[0][0] + step, marks[1][0], step)]
    for ts_k, lab in marks:
        ax.axvspan(xp(ts_k) - 0.45, xp(ts_k) + 0.45, color=C_HI, alpha=0.35, zorder=1)
        if lab:
            ax.annotate(lab, (xp(ts_k), float(t["extreme"])), xytext=(-8, -14 if d > 0 else 8),
                        textcoords="offset points", ha="right", fontsize=8, color=INK)  # fmt: skip
    x_end = float(x[-1]) + 0.5
    xe = xp(int(t["entry_ts"]))
    lines = [(t["entry"], C_ENTRY, "entry", "-"), (t["stop"], C_STOP, "stop", "-"),
             (t["tp1"], C_TGT, "TP1", "-")]  # fmt: skip
    if np.isfinite(t["tp2"]) and t["exit2"] == "lvl":
        lines.append((t["tp2"], C_TGT, "TP2", "--"))
    if t["trigger"] == "T1" and np.isfinite(t["swing"]):
        lines.append((t["swing"], C_SWING, "15m swing", ":"))
    for y, col, lab, ls in lines:
        x0 = 0 if lab == "15m swing" else xe
        ax.hlines(y, x0, x_end, colors=col, lw=1.6 if ls == "-" else 1.2, linestyles=ls, zorder=4)  # type: ignore[arg-type]
        ax.text(x_end + 0.3, y, f"{lab} {y:,.6g}", va="center", fontsize=8, color=INK)
    poi_txt: list[str] = []
    for p in str(t["pois"]).split(";"):
        kind, rest = p.split(":")
        near, far = (float(v) for v in rest.split("/"))
        if near == far:
            ax.hlines(near, 0, x_end, colors=C_POI, lw=1.2, linestyles="--", zorder=4)
        else:
            ax.axhspan(min(near, far), max(near, far), color=C_POI, alpha=0.15, zorder=0)
        poi_txt.append(f"{kind} {near:,.6g}" + ("" if near == far else f"–{far:,.6g}"))
    ax.text(0.01, 0.02, "POI (orange): " + "; ".join(poi_txt) + f"; stop anchor (far edge) {t['far']:,.6g}",
            transform=ax.transAxes, fontsize=8, color=INK, va="bottom",
            bbox={"facecolor": "white", "alpha": 0.8, "lw": 0})  # fmt: skip
    for bars, per, col, lab in ((b4, m4.H4, C_EMA, "4H EMA50 (at 4H close)"),
                                (bd, m4.DAY, C_DEMA, "daily EMA50 (at daily close)")):  # fmt: skip
        e = m5.ema(bars.c, i5.ema_period)
        ct = bars.ts + per
        sel = (ct >= bb.ts[0]) & (ct <= bb.ts[-1] + step)
        if sel.any():
            # also draw the value in force at the chart start
            k0 = int(np.searchsorted(ct, bb.ts[0], side="right")) - 1
            xs = ([xp(int(bb.ts[0]))] if k0 >= 0 else []) + [xp(int(v)) for v in ct[sel]]
            ys = ([e[k0]] if k0 >= 0 else []) + list(e[sel])
            ax.step(xs, ys, where="post", color=col, lw=1.4, label=lab, zorder=4)
    for xts, xpx, lab in ((t["x1_ts"], t["x1_px"], "exit 1"), (t["x2_ts"], t["x2_px"], "exit 2")):
        xx = xp(int(xts) // step * step)
        ax.plot(xx, xpx, marker="X", ms=9, color=INK, zorder=6)
        if not (lab == "exit 2" and t["x1_ts"] == t["x2_ts"] and t["x1_px"] == t["x2_px"]):
            ax.annotate(lab, (xx, xpx), xytext=(8, 5), ha="left", textcoords="offset points",
                        fontsize=8, color=INK)  # fmt: skip
    ax.axvline(xe - 0.5, color=C_ENTRY, lw=0.8, alpha=0.6)
    every = 16
    ticks = np.arange(0, len(x), every)
    ax.set_xticks(ticks)
    ax.set_xticklabels([_utc(int(bb.ts[i])) for i in ticks], fontsize=8, color=INK2)
    ax.tick_params(axis="y", labelsize=8, colors=INK2)
    ax.grid(True, color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.set_xlim(-1, x_end + 10)
    side = "long" if d > 0 else "short"
    ax.set_title(
        f"{title}\nbias {side}: 4H b1, entry {t['entry']:,.6g} vs 4H EMA50 {t['ema4']:,.6g}, "
        f"daily close {t['close_d']:,.6g} vs daily EMA50 {t['emad']:,.6g}; "
        f"{'15m' if step == m4.M15 else '30m'} candles",
        fontsize=9.5,
        color=INK,
        loc="left",
    )
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def example_trades(
    tr_raw: pl.DataFrame, sel_key: str, cfg: AppConfig, kroot: Path, out_dir: Path
) -> str:
    tr = apply_costs(tr_raw.filter(pl.col("config") == sel_key), cfg, only=("default",))
    tr = tr.sort("net_r_default", "entry_ts")
    n = tr.height
    if n == 0:
        return "No trades to show.\n"
    picks: list[tuple[str, int]] = [("best", n - 1 - i) for i in range(min(3, n))]
    picks += [("worst", i) for i in range(min(3, n))]
    mid = n // 2
    picks += [("median", i) for i in (mid - 2, mid - 1, mid, mid + 1) if 0 <= i < n]
    seen: set[int] = set()
    picks = [(k, i) for k, i in picks if not (i in seen or seen.add(i))]  # type: ignore[func-returns-value]
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("trade-*.png"):
        old.unlink()
    cache: dict[tuple[str, str], pl.DataFrame] = {}
    L = []
    for n_ex, (kind, i) in enumerate(picks, 1):
        t = tr.row(i, named=True)
        key = (t["window"], t["symbol"])
        if key not in cache:
            cache[key] = load_window5(kroot, t["symbol"], cfg, t["window"])
        side = "LONG" if t["direction"] > 0 else "SHORT"
        title = (f"#{n_ex} {kind}: {t['symbol']} {side} {_utc(t['entry_ts'], '%Y-%m-%d %H:%M')} UTC, "
                 f"window {t['window']}, {t['config']}, net {t['net_r_default']:+.2f} R")  # fmt: skip
        fn = f"trade-{n_ex:02d}-{kind}.png"
        chart_trade(t, cache[key], cfg, out_dir / fn, title)
        labs = (
            ("sweep bar", "close-back bar")
            if t["trigger"] == "T1"
            else ("touch bar", "confirmation bar")
        )
        tp2 = f", TP2 {t['tp2']:,.6g}" if np.isfinite(t["tp2"]) else ", no TP2"
        sa = t.get("stop_anchor")
        anchor = (
            f"confirmation-candle wick {sa:,.6g}"
            if sa is not None and np.isfinite(sa)
            else f"far edge {t['far']:,.6g}"
        )
        L += [
            f"### {n_ex}. {kind} — {t['symbol']} {side}, entry {_utc(t['entry_ts'], '%Y-%m-%d %H:%M')} UTC (window {t['window']})",
            "",
            f"![trade {n_ex}]({out_dir.name}/{fn})",
            "",
            f"- POI(s): `{t['pois']}`; {labs[0]} {_utc(t['a_ts'])}, {labs[1]} {_utc(t['b_ts'])}, extreme {t['extreme']:,.6g}"
            + (f", 15m swing {t['swing']:,.6g}" if t["trigger"] == "T1" else ""),
            f"- bias: entry {t['entry']:,.6g} vs 4H EMA50 {t['ema4']:,.6g}; daily close {t['close_d']:,.6g} vs daily EMA50 {t['emad']:,.6g}",
            f"- stop {t['stop']:,.6g} = {anchor} ± {t['k']:g} × ATR(1h) {t['atr1h']:,.4g} (or the minimum distance): "
            f"{t['r_unit'] / t['entry'] * 1e4:.0f} bps; TP1 {t['tp1']:,.6g} ({t['tp1_r']:.2f} R){tp2}",
            f"- exit 1 {t['x1_px']:,.6g} at {_utc(t['x1_ts'])}, exit 2 {t['x2_px']:,.6g} at {_utc(t['x2_ts'])} "
            f"({t['exit_kind']}); gross {t['gross_r']:+.2f} R, net {t['net_r_default']:+.2f} R ({t['bps_default']:+.1f} bps)",
            "",
        ]
    return "\n".join(L) + "\n"
