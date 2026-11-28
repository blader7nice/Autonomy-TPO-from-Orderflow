"""Costs, grid selection and REPORT.md for a tuning run (Phase 3).

Selection rule (pre-registered in DECISIONS D-036 before the first run): over the grid
resolution x W x threshold, take the static-zone paired uplift (treatment - unconfirmed,
both symbols pooled) at the primary exit with the default cost scenario; keep configs with
at least min_n_per_group trades in each group; choose the highest lower bound of the
session-clustered 90% CI; ties -> more treatment trades.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict
from typing import Any

import numpy as np
import polars as pl

from flowlevels.config import AppConfig
from flowlevels.research.stats import (
    GroupStats,
    Uplift,
    group_stats,
    mde,
    paired_uplift,
    weekly_positive_share,
)
from flowlevels.signals.detectors import DETECTORS

DAY_MS = 86_400_000


def cost_scenarios(cfg: AppConfig) -> dict[str, dict[str, Any]]:
    """name -> {fee profile, slippage scenario, maker entry}. 'default' is the primary one."""
    c = cfg.costs
    out: dict[str, dict[str, Any]] = {
        "default": {"fees": c.fee_profile, "slip": c.slippage, "maker_entry": False},
        "maker_entry": {"fees": c.fee_profile, "slip": c.slippage, "maker_entry": True},
    }
    for s in c.slippage_scenarios:
        if s != c.slippage:
            out[f"slip_{s}"] = {"fees": c.fee_profile, "slip": s, "maker_entry": False}
    for f in c.fee_profiles:
        if f != c.fee_profile:
            out[f"fees_{f}"] = {"fees": f, "slip": c.slippage, "maker_entry": False}
    return out


def apply_costs(
    trades: pl.DataFrame, cfg: AppConfig, only: tuple[str, ...] | None = None
) -> pl.DataFrame:
    """Add net_r_<scenario> and bps_<scenario> for every cost scenario (or `only` those),
    valid trades only."""
    t = trades.filter(pl.col("valid"))
    sym_bucket = {s: cfg.symbol(s).bucket for s in cfg.symbols}
    sym_tick = {s: i.tick for s, i in cfg.costs.execution_instruments.items()}
    cols = []
    for name, sc in cost_scenarios(cfg).items():
        if only is not None and name not in only:
            continue
        fp = cfg.costs.fee_profiles[sc["fees"]]
        ss = cfg.costs.slippage_scenarios[sc["slip"]]
        unit = sym_bucket if ss.unit == "bucket" else sym_tick
        slip = (
            pl.col("symbol").cast(pl.String).replace_strict(unit, return_dtype=pl.Float64)
            * ss.amount
        )
        d = pl.col("direction").cast(pl.Float64)
        e_fill = pl.col("entry") + d * (0.0 if sc["maker_entry"] else 1.0) * slip
        x_fill = pl.col("exit_px") - d * slip
        fe = fp.maker_fee if sc["maker_entry"] else fp.taker_fee
        net = d * (x_fill - e_fill) - fe * e_fill - fp.taker_fee * x_fill
        cols += [
            (net / pl.col("r_unit")).alias(f"net_r_{name}"),
            (net / pl.col("entry") * 1e4).alias(f"bps_{name}"),
        ]
    return t.with_columns(cols).with_columns(
        pl.from_epoch("session", time_unit="ms").dt.strftime("%G-W%V").alias("week")
    )


def _session_key(df: pl.DataFrame, cfg: AppConfig) -> np.ndarray:
    """Session (symbol, UTC day) as an int; the symbol index is fixed by the config."""
    sym = {s: i for i, s in enumerate(sorted(cfg.symbols))}
    key: np.ndarray = (
        df["symbol"].cast(pl.String).replace_strict(sym, return_dtype=pl.Int64).to_numpy() * 10**6
        + df["session"].to_numpy() // DAY_MS
    )
    return key


def uplift_of(t: pl.DataFrame, u: pl.DataFrame, cfg: AppConfig, scen: str = "default") -> Uplift:
    rc = cfg.research
    return paired_uplift(
        t[f"net_r_{scen}"].to_numpy(),
        _session_key(t, cfg),
        u[f"net_r_{scen}"].to_numpy(),
        _session_key(u, cfg),
        rc.bootstrap_reps,
        rc.ci_level,
        rc.seed,
    )


def stats_of(df: pl.DataFrame, scen: str = "default") -> GroupStats:
    return group_stats(
        df[f"net_r_{scen}"].to_numpy(),
        df[f"bps_{scen}"].to_numpy(),
        df["gross_r"].to_numpy(),
        df["mfe_r"].to_numpy(),
        df["mae_r"].to_numpy(),
        df["entry_ts"].to_numpy(),
    )


def grid_table(tr: pl.DataFrame, cfg: AppConfig) -> pl.DataFrame:
    pe = cfg.trade_model.primary_exit
    x = apply_costs(
        tr.filter(
            (pl.col("pool") == "static")
            & (pl.col("exit") == pe)
            & pl.col("group").is_in(["treatment", "unconfirmed"])
        ),
        cfg,
        only=("default",),
    )
    rows = []
    for res in cfg.signals.resolutions:
        for w in cfg.research.grid_window_bars:
            for thr in cfg.research.grid_score_threshold:
                sel = x.filter((pl.col("res") == res) & (pl.col("w") == w) & (pl.col("thr") == thr))
                t, u = (
                    sel.filter(pl.col("group") == "treatment"),
                    sel.filter(pl.col("group") == "unconfirmed"),
                )
                up = uplift_of(t, u, cfg)
                rows.append({"res": res, "w": w, "thr": thr, **asdict(up)})
    g = pl.DataFrame(rows)
    mn = cfg.research.min_n_per_group
    elig = g.filter((pl.col("n_t") >= mn) & (pl.col("n_u") >= mn) & pl.col("lo").is_not_nan())
    best = elig.sort(["lo", "n_t"], descending=True).head(1)
    sel_key = (best["res"][0], best["w"][0], best["thr"][0]) if best.height else None
    return g.with_columns(
        (
            (pl.col("res") == (sel_key[0] if sel_key else ""))
            & (pl.col("w") == (sel_key[1] if sel_key else -1))
            & (pl.col("thr") == (sel_key[2] if sel_key else -1.0))
        ).alias("selected"),
        ((pl.col("n_t") >= mn) & (pl.col("n_u") >= mn)).alias("eligible"),
    )


def _f(x: float, nd: int = 3) -> str:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "–" if x is None or np.isnan(x) else ("inf" if x > 0 else "-inf")
    return f"{x:+.{nd}f}" if nd else f"{x:.0f}"


def _stats_row(name: str, s: GroupStats) -> str:
    if s.n == 0:
        return f"| {name} | 0 | | | | | | | | | |"
    return (
        f"| {name} | {s.n} | {s.win_rate:.1%} | {_f(s.exp_r)} | {s.exp_bps:+.1f} | "
        f"{s.pf:.2f} | {s.mfe_r:.2f} | {s.mae_r:.2f} | {s.max_dd_r:.1f} | {s.cost_r:.3f} | "
        f"{s.q[0]:+.2f} / {s.q[2]:+.2f} / {s.q[4]:+.2f} |"
    )


STATS_HEAD = (
    "| group | N | win | exp R | exp bps | PF | MFE R | MAE R | max DD R | cost R | R p5 / p50 / p95 |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|"
)


def _up_row(name: str, u: Uplift) -> str:
    return (
        f"| {name} | {u.n_t} | {u.n_u} | {_f(u.mean_t)} | {_f(u.mean_u)} | **{_f(u.uplift)}** | "
        f"[{_f(u.lo)}, {_f(u.hi)}] | {u.n_sessions} |"
    )


UP_HEAD = (
    "| slice | N treat | N unconf | exp treat R | exp unconf R | uplift R | 90% CI | sessions |\n"
    "|---|---|---|---|---|---|---|---|"
)


def render(
    tr_raw: pl.DataFrame, meta: dict[str, Any], cfg: AppConfig
) -> tuple[str, dict[str, Any]]:
    tr = tr_raw  # costs are applied per slice (memory: millions of rows)
    rc, tm = cfg.research, cfg.trade_model
    pe = tm.primary_exit
    grid = grid_table(tr, cfg)
    n_cfg = grid.height
    sel = grid.filter(pl.col("selected"))
    L: list[str] = []
    a = L.append
    a(f"# Phase 3 tuning-window report (in-sample) — run `{meta['run_id']}`")
    a("")
    a(
        f"Commit `{meta['commit']}`, config `{meta['config_hash']}` (v{cfg.version}), "
        f"created {meta['created_utc']}. Window **{meta['days'][0]} … {meta['days'][1]}** "
        f"({meta['n_days']} days). Symbols: {', '.join(meta['symbols'])}. Signals: Binance "
        f"USD-M; execution: OKX EEA {', '.join(i.inst_id for i in cfg.costs.execution_instruments.values())}."
    )
    a("")
    a(
        f"Costs (default): fee profile `{cfg.costs.fee_profile}` (taker {cfg.costs.taker_fee:.2%} "
        f"per side, **UNCONFIRMED** for the owner's account), slippage `{cfg.costs.slippage}` "
        "(1 footprint bucket per side: 10 USD BTC, 0.5 USD ETH). All results are net of costs."
    )
    a("")
    a(
        "**This is an in-sample report. It is not a verdict. The hold-out has not been run (0 runs).**"
    )
    a("")
    a("## 1. Data")
    a("")
    a(f"- Usable sessions (clean, with ATR at the open): {meta['sessions']}.")
    a(f"- Repaired holes: {meta['repairs'] or 'none'}.")
    a(f"- {meta['exclusions']}")
    a(f"- Run time {meta['seconds_total']} s ({meta['timing_s']}).")
    a("")
    a("## 2. Grid and selection")
    a("")
    a(
        f"**Configurations tried: {n_cfg}** = {len(cfg.signals.resolutions)} resolutions × "
        f"{len(rc.grid_window_bars)} W × {len(rc.grid_score_threshold)} thresholds. Detector "
        "thresholds, weights, stops and exits were fixed before any outcome was seen and not "
        "tuned. Sensitivities (costs, zone-edge stop, other exits, dynamic zones) are reported "
        "but never used for selection."
    )
    a("")
    a(
        f"Metric: static zones, exit `{pe}`, default costs, BTC+ETH pooled; uplift = treatment − "
        f"unconfirmed, session-clustered bootstrap ({rc.bootstrap_reps} reps, seed {rc.seed}). "
        f"Rule (D-036): max lower CI bound among configs with N ≥ {rc.min_n_per_group} per group."
    )
    a("")
    a("| res | W | thr | N treat | N unconf | exp treat | exp unconf | uplift | 90% CI | sel |")
    a("|---|---|---|---|---|---|---|---|---|---|")
    for g in grid.iter_rows(named=True):
        mark = "**◀**" if g["selected"] else ("" if g["eligible"] else "n<min")
        a(
            f"| {g['res']} | {g['w']} | {g['thr']:g} | {g['n_t']} | {g['n_u']} | {_f(g['mean_t'])} | "
            f"{_f(g['mean_u'])} | {_f(g['uplift'])} | [{_f(g['lo'])}, {_f(g['hi'])}] | {mark} |"
        )
    a("")
    out: dict[str, Any] = {"n_configs": n_cfg, "grid": grid.to_dicts()}
    if sel.height == 0:
        a("**No configuration has enough trades in both groups. Nothing selected.**")
        return "\n".join(L) + "\n", out
    res, w, thr = sel["res"][0], int(sel["w"][0]), float(sel["thr"][0])
    out["selected"] = {"res": res, "w": w, "thr": thr}
    a(
        f"**Selected: resolution `{res}`, W = {w}, threshold = {thr:g}.** With {n_cfg} configs "
        "tried, the selected in-sample uplift is optimistically biased (winner's curse); only the "
        "hold-out can say whether it holds."
    )
    a("")
    grp_tuw = pl.col("group").is_in(["treatment", "treatment_zonestop", "unconfirmed"])
    S = apply_costs(
        tr.filter(
            (pl.col("res") == res)
            & ((grp_tuw & (pl.col("w") == w) & (pl.col("thr") == thr)) | ~grp_tuw)
        ),
        cfg,
    )
    del tr

    def grp(df: pl.DataFrame, g: str) -> pl.DataFrame:
        if g in ("treatment", "treatment_zonestop", "unconfirmed"):
            return df.filter((pl.col("group") == g) & (pl.col("w") == w) & (pl.col("thr") == thr))
        return df.filter(pl.col("group") == g)

    for pool in ("static", "dynamic"):
        P = S.filter(
            (pl.col("pool") == pool) | ((pl.col("group") == "random") & (pool == "static"))
        )
        title = (
            "3. Static zones (primary)"
            if pool == "static"
            else "4. Dynamic zones (separate, not used for selection)"
        )
        a(f"## {title}")
        a("")
        for sym in [*meta["symbols"], "pooled"]:
            X = P if sym == "pooled" else P.filter(pl.col("symbol") == sym)
            X = X.filter(pl.col("exit") == pe)
            a(f"**{sym}**, exit `{pe}`, default costs")
            a("")
            a(STATS_HEAD)
            for gname in ("treatment", "unconfirmed", "baseline", "random", "treatment_zonestop"):
                if gname == "random" and pool == "dynamic":
                    continue
                a(_stats_row(gname, stats_of(grp(X, gname))))
            a("")
        X = P
        a(f"Uplift by exit (pooled, default costs, {pool} zones):")
        a("")
        a(UP_HEAD)
        for e in [f"{k:g}R" for k in tm.targets_r] + ["zone", "time"]:
            Y = X.filter(pl.col("exit") == e)
            a(_up_row(f"exit {e}", uplift_of(grp(Y, "treatment"), grp(Y, "unconfirmed"), cfg)))
        a("")
        a(
            "The `time` exit has no target, so its mean is sensitive to single trades with a small "
            "stop (minimum stop 0.1 × ATR) during a large move; read it with the R quantiles."
        )
        a("")

    P = S.filter((pl.col("pool") == "static") & (pl.col("exit") == pe))
    T, U = grp(P, "treatment"), grp(P, "unconfirmed")
    up = uplift_of(T, U, cfg)
    a("## 5. Breakdowns (static, selected config, primary exit)")
    a("")
    a(UP_HEAD)
    a(_up_row("all", up))
    for sym in meta["symbols"]:
        a(
            _up_row(
                sym,
                uplift_of(
                    T.filter(pl.col("symbol") == sym), U.filter(pl.col("symbol") == sym), cfg
                ),
            )
        )
    for tn, cond in (
        ("touch 1", pl.col("touch_n") == 1),
        ("touch 2", pl.col("touch_n") == 2),
        ("touch 3+", pl.col("touch_n") >= 3),
    ):
        a(_up_row(tn, uplift_of(T.filter(cond), U.filter(cond), cfg)))
    for ctx in ("up", "down", "range"):
        c = pl.col("context") == ctx
        a(_up_row(f"context {ctx}", uplift_of(T.filter(c), U.filter(c), cfg)))
    for dc in (True, False):
        c = pl.col("dyn_conf") == dc
        a(
            _up_row(
                f"dynamic confluence {'yes' if dc else 'no'}",
                uplift_of(T.filter(c), U.filter(c), cfg),
            )
        )
    a("")
    a(
        "By detector (treatment trades where the detector fired by the signal bar vs all unconfirmed):"
    )
    a("")
    a(UP_HEAD)
    for i, d in enumerate(DETECTORS):
        a(_up_row(d, uplift_of(T.filter((pl.col("detectors") & (1 << i)) > 0), U, cfg)))
    a("")
    a("By anchor level type:")
    a("")
    a(UP_HEAD)
    for at in sorted(set(T["anchor_type"].to_list()) | set(U["anchor_type"].to_list())):
        c = pl.col("anchor_type") == at
        a(_up_row(at, uplift_of(T.filter(c), U.filter(c), cfg)))
    a("")
    share, nw = weekly_positive_share(
        T["net_r_default"].to_numpy(),
        T["week"].to_numpy(),
        U["net_r_default"].to_numpy(),
        U["week"].to_numpy(),
    )
    a("By week:")
    a("")
    a(UP_HEAD)
    for wk in sorted(set(T["week"].to_list()) | set(U["week"].to_list())):
        c = pl.col("week") == wk
        a(_up_row(wk, uplift_of(T.filter(c), U.filter(c), cfg)))
    a("")
    a(f"Uplift positive in **{share:.0%} of {nw} weeks**.")
    a("")
    a("## 6. Cost scenarios (static, selected config, primary exit, pooled)")
    a("")
    a(
        "| scenario | fees | slippage | exp treat R | exp treat bps | exp unconf R | uplift R | 90% CI | cost R treat | cost R unconf |"
    )
    a("|---|---|---|---|---|---|---|---|---|---|")
    for name, sc in cost_scenarios(cfg).items():
        u2 = uplift_of(T, U, cfg, name)
        st, su = stats_of(T, name), stats_of(U, name)
        a(
            f"| {name} | {sc['fees']}{' (maker entry)' if sc['maker_entry'] else ''} | {sc['slip']} | "
            f"{_f(u2.mean_t)} | {st.exp_bps:+.1f} | {_f(u2.mean_u)} | {_f(u2.uplift)} | "
            f"[{_f(u2.lo)}, {_f(u2.hi)}] | {st.cost_r:.3f} | {su.cost_r:.3f} |"
        )
    a("")
    a(
        "The maker-entry row assumes every limit entry fills at the bar open: an optimistic, labelled sensitivity."
    )
    a("")
    # random control sanity check
    R = S.filter((pl.col("group") == "random") & (pl.col("exit") == pe))
    rs = stats_of(R)
    a("## 7. Random control")
    a("")
    a(
        f"N = {rs.n}, expectancy {_f(rs.exp_r)} R ({rs.exp_bps:+.1f} bps), cost {rs.cost_r:.3f} R. "
        + (
            "**Positive net expectancy in random entries: treat as a bug signal (AGENTS.md).**"
            if rs.n and rs.exp_r > 0
            else "Not positive, as expected for random direction after costs."
        )
    )
    a("")
    # MDE
    n_tune = up.n_sessions
    n_hold = rc.holdout_days * len(meta["symbols"])
    m = mde(up.se, n_tune, n_hold, rc.ci_level, rc.power) if np.isfinite(up.se) else float("nan")
    per_sess_t = up.n_t / n_tune if n_tune else float("nan")
    per_sess_u = up.n_u / n_tune if n_tune else float("nan")
    a("## 8. MDE for the hold-out (Phase 3b input)")
    a("")
    a(
        f"Tuning: uplift SE (session bootstrap) = {up.se:.3f} R over {n_tune} sessions "
        f"({per_sess_t:.2f} treatment and {per_sess_u:.2f} unconfirmed trades per session). "
        f"Hold-out: {rc.holdout_days} days × {len(meta['symbols'])} symbols = {n_hold} sessions, "
        f"so the expected N is about {per_sess_t * n_hold:.0f} treatment and {per_sess_u * n_hold:.0f} "
        f"unconfirmed trades, if the event rate is the same."
    )
    a("")
    a(
        f"MDE (one-sided α = {(1 - rc.ci_level) / 2:.2f}, matching the 90% CI gate; power "
        f"{rc.power:.0%}) = **{m:.3f} R**. Gate uplift = +{rc.gate_uplift_r} R → "
        + (
            "**adequately powered** (MDE ≤ gate)."
            if m <= rc.gate_uplift_r
            else "**NOT adequately powered** (MDE > gate)."
        )
    )
    a("")
    a(
        "Caveats: the SE comes from 60 days of one regime; unrepaired holes in the 9 months would "
        "reduce N (none scanned yet); the hold-out pipeline must repair or exclude holes first."
    )
    a("")
    a("## 9. In-sample gate preview (NOT a verdict)")
    a("")
    st = stats_of(T)
    checks = {
        "treatment expectancy > 0R": st.exp_r > 0,
        f"uplift ≥ +{rc.gate_uplift_r}R": up.uplift >= rc.gate_uplift_r,
        "90% CI lower bound > 0": up.lo > 0,
        "uplift positive in ≥ 60% of weeks": share >= 0.6,
    }
    for k, v in checks.items():
        a(f"- {k}: {'yes' if v else 'no'}")
    a("")
    a("## 10. Frozen config proposal for the hold-out (owner review)")
    a("")
    a(
        f"`signals.primary_resolution = {res}`, `signals.window_bars = {w}`, "
        f"`signals.score_threshold = {thr:g}`; everything else as in config `{meta['config_hash']}`. "
        "Fees must be replaced by the owner's actual rates **before** the hold-out (a fee change "
        "after the hold-out would count as a re-run)."
    )
    out |= {
        "uplift": asdict(up),
        "mde": m,
        "weeks_positive": share,
        "n_weeks": nw,
        "random": asdict(rs),
        "gate_preview": checks,
    }
    return "\n".join(L) + "\n", out


def created() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
