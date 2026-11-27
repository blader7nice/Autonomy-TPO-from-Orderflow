"""ITERATION-1 report: grid, pre-registered selection, kill criterion, details."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import numpy as np
import polars as pl

from flowlevels.config import AppConfig
from flowlevels.research.iteration1 import PRIMARY_EXIT
from flowlevels.research.report import (
    STATS_HEAD,
    UP_HEAD,
    _f,
    _stats_row,
    _up_row,
    apply_costs,
    cost_scenarios,
    stats_of,
    uplift_of,
)
from flowlevels.research.stats import mde, weekly_positive_share

CFG_KEYS = ("thr", "w", "k_atr")


def _cfg_filter(thr: float, w: int, k: float) -> pl.Expr:
    grid = pl.col("group").is_in(["treatment", "unconfirmed"])
    same_k = pl.col("k_atr") == k
    return same_k & (~grid | ((pl.col("thr") == thr) & (pl.col("w") == w)))


def grid_table(tr: pl.DataFrame, cfg: AppConfig) -> pl.DataFrame:
    it = cfg.iteration1
    x = tr.filter(pl.col("exit") == PRIMARY_EXIT)
    rows = []
    for thr in it.grid_score_threshold:
        for w in it.grid_window_bars:
            for k in it.grid_min_stop_k_atr:
                sel = x.filter(_cfg_filter(thr, w, k))
                t = sel.filter(pl.col("group") == "treatment")
                u = sel.filter(pl.col("group") == "unconfirmed")
                st = stats_of(t)
                up = uplift_of(t, u, cfg)
                rows.append(
                    {
                        "thr": thr, "w": w, "k_atr": k, "n_t": st.n, "exp_r": st.exp_r,
                        "exp_bps": st.exp_bps, "win": st.win_rate, "pf": st.pf, "max_dd": st.max_dd_r,
                        "cost_r": st.cost_r, "n_u": up.n_u, "exp_u": up.mean_u, "uplift": up.uplift,
                        "lo": up.lo, "hi": up.hi,
                    }
                )  # fmt: skip
    g = pl.DataFrame(rows)
    elig = g.filter(pl.col("n_t") >= it.min_treatment_trades)
    best = elig.sort(["exp_r", "max_dd"], descending=[True, False]).head(1)
    key = best.select(CFG_KEYS).row(0) if best.height else None
    sel_expr = pl.lit(False)
    if key:
        sel_expr = (pl.col("thr") == key[0]) & (pl.col("w") == key[1]) & (pl.col("k_atr") == key[2])
    return g.with_columns(
        sel_expr.alias("selected"), (pl.col("n_t") >= it.min_treatment_trades).alias("eligible")
    )


def render(
    tr_raw: pl.DataFrame, meta: dict[str, Any], cfg: AppConfig
) -> tuple[str, dict[str, Any]]:
    it, rc = cfg.iteration1, cfg.research
    tr = apply_costs(tr_raw, cfg)
    grid = grid_table(tr, cfg)
    L: list[str] = []
    a = L.append
    a(f"# ITERATION-1 in-sample report — run `{meta['run_id']}`")
    a("")
    a(
        f"Commit `{meta['commit']}`, config `{meta['config_hash']}` (v{cfg.version}), created "
        f"{meta['created_utc']}. Window **{meta['days'][0]} … {meta['days'][1]}** ({meta['n_days']} "
        f"days), {', '.join(meta['symbols'])}, sessions {meta['sessions']}. Pre-registered in "
        "DECISIONS `ITERATION-1` (commit 4e1f1f0) before any code or run."
    )
    a("")
    a(
        f"Rules: 5m signals; first touch of a static zone with score ≥ **{meta['min_zone_score_used']:g}** "
        f"(frozen in-sample median of all static zone scores = {meta['zone_score_median']:g}); stop beyond "
        f"the decision bar + 2 ticks, ≥ max(k × ATR(1h), {it.min_stop_cost_mult:g} × round-trip cost); "
        f"target = nearest other static zone, taken only if ≥ {it.min_target_r:g} × stop; time stop "
        f"{it.time_stop_bars} bars (4 h). Costs: `{cfg.costs.fee_profile}` (taker {cfg.costs.taker_fee:.2%}, "
        f"UNCONFIRMED) + `{cfg.costs.slippage}` slippage per side. Setups: {meta['setups']}."
    )
    a("")
    a("**In-sample only. The hold-out has not been run (0 runs).**")
    a("")
    a("## 1. Grid (all 12 configurations) and selection")
    a("")
    a(
        f"Configurations in this iteration: **{grid.height}**; tuning-window configurations in "
        f"total including the Phase 3 run: **{36 + grid.height}**. Rule: highest treatment net "
        f"expectancy (R, exit `{PRIMARY_EXIT}`, default costs, pooled) among configs with "
        f"N ≥ {it.min_treatment_trades}; tie → lower max drawdown."
    )
    a("")
    a(
        "| thr | W | k·ATR | N treat | exp R | exp bps | win | PF | max DD R | cost R | N unconf | exp unconf R | uplift | 90% CI | sel |"
    )
    a("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for g in grid.iter_rows(named=True):
        mark = "**◀**" if g["selected"] else ("" if g["eligible"] else "n<min")
        a(
            f"| {g['thr']:g} | {g['w']} | {g['k_atr']:g} | {g['n_t']} | {_f(g['exp_r'])} | "
            f"{g['exp_bps']:+.1f} | {g['win']:.1%} | {g['pf']:.2f} | {g['max_dd']:.1f} | "
            f"{g['cost_r']:.3f} | {g['n_u']} | {_f(g['exp_u'])} | {_f(g['uplift'])} | "
            f"[{_f(g['lo'])}, {_f(g['hi'])}] | {mark} |"
        )
    a("")
    out: dict[str, Any] = {"grid": grid.to_dicts(), "n_configs": grid.height}
    sel = grid.filter(pl.col("selected"))
    if sel.height == 0:
        out["verdict"] = "KILL"
        a(
            f"**No configuration reaches {it.min_treatment_trades} treatment trades → nothing can be selected → KILL** (pre-registered interpretation)."
        )
        return "\n".join(L) + "\n", out
    thr, w, k = sel.select(CFG_KEYS).row(0)
    exp_r = float(sel["exp_r"][0])
    verdict = "KILL" if exp_r <= 0 else "POSITIVE"
    out |= {"selected": {"thr": thr, "w": w, "k_atr": k}, "exp_r": exp_r, "verdict": verdict}
    a(
        f"**Selected: threshold {thr:g}, W = {w}, k = {k:g} × ATR(1h). Net expectancy {exp_r:+.3f} R.**"
    )
    a("")
    a("## 2. Kill criterion (pre-registered)")
    a("")
    if verdict == "KILL":
        a(
            f"Selected config net expectancy = **{exp_r:+.3f} R ≤ 0 R** with default OKX costs → "
            "**the orderflow-confirmation trading thesis is KILLED.** The hold-out is not run."
        )
    else:
        a(
            f"Selected config net expectancy = **{exp_r:+.3f} R > 0 R** → not killed; proceed to the reporting/preparation steps, STOP before the hold-out."
        )
    a("")
    S = tr.filter(_cfg_filter(thr, w, k))
    a("## 3. Groups (selected config)")
    a("")
    for sym in [*meta["symbols"], "pooled"]:
        X = S if sym == "pooled" else S.filter(pl.col("symbol") == sym)
        X = X.filter(pl.col("exit") == PRIMARY_EXIT)
        a(f"**{sym}**, exit `{PRIMARY_EXIT}`, default costs")
        a("")
        a(STATS_HEAD)
        for gname in ("treatment", "unconfirmed", "baseline", "random"):
            a(_stats_row(gname, stats_of(X.filter(pl.col("group") == gname))))
        a("")
    a("Uplift (treatment − unconfirmed, pooled, default costs) by exit; 2R/3R are secondary:")
    a("")
    a(UP_HEAD)
    for e in [PRIMARY_EXIT, *it.secondary_exits]:
        Y = S.filter(pl.col("exit") == e)
        a(
            _up_row(
                f"exit {e}",
                uplift_of(
                    Y.filter(pl.col("group") == "treatment"),
                    Y.filter(pl.col("group") == "unconfirmed"),
                    cfg,
                ),
            )
        )
    a("")
    P = S.filter(pl.col("exit") == PRIMARY_EXIT)
    T, U = P.filter(pl.col("group") == "treatment"), P.filter(pl.col("group") == "unconfirmed")
    up = uplift_of(T, U, cfg)
    st = stats_of(T)
    share, nw = weekly_positive_share(
        T["net_r_default"].to_numpy(),
        T["week"].to_numpy(),
        U["net_r_default"].to_numpy(),
        U["week"].to_numpy(),
    )
    a("## 4. Weekly stability (selected config, primary exit)")
    a("")
    a("| week | N treat | exp treat R | N unconf | exp unconf R |")
    a("|---|---|---|---|---|")
    weeks_pos_exp = 0
    weeks = sorted(set(T["week"].to_list()) | set(U["week"].to_list()))
    for wk in weeks:
        t_, u_ = T.filter(pl.col("week") == wk), U.filter(pl.col("week") == wk)
        mt = float(t_["net_r_default"].mean()) if t_.height else float("nan")  # type: ignore[arg-type]
        mu = float(u_["net_r_default"].mean()) if u_.height else float("nan")  # type: ignore[arg-type]
        weeks_pos_exp += t_.height > 0 and mt > 0
        a(f"| {wk} | {t_.height} | {_f(mt)} | {u_.height} | {_f(mu)} |")
    a("")
    a(
        f"Treatment expectancy positive in {weeks_pos_exp} of {len(weeks)} weeks; uplift positive in {share:.0%} of {nw} weeks."
    )
    a("")
    a("## 5. Cost sensitivity (selected config, primary exit, pooled; the trades are identical)")
    a("")
    a(
        "| scenario | fees | slippage | N | exp treat R | exp treat bps | cost R | exp unconf R | uplift R | 90% CI |"
    )
    a("|---|---|---|---|---|---|---|---|---|---|")
    for name, sc in cost_scenarios(cfg).items():
        s2, u2 = stats_of(T, name), uplift_of(T, U, cfg, name)
        a(
            f"| {name} | {sc['fees']}{' (maker entry)' if sc['maker_entry'] else ''} | {sc['slip']} | {s2.n} | "
            f"{_f(s2.exp_r)} | {s2.exp_bps:+.1f} | {s2.cost_r:.3f} | {_f(u2.mean_u)} | {_f(u2.uplift)} | [{_f(u2.lo)}, {_f(u2.hi)}] |"
        )
    a("")
    R = P.filter(pl.col("group") == "random")
    rs = stats_of(R)
    a("## 6. Random control and geometry")
    a("")
    a(
        f"Random (same rules, random direction): N = {rs.n}, {_f(rs.exp_r)} R ({rs.exp_bps:+.1f} bps), cost {rs.cost_r:.3f} R"
        + (
            " — **positive: bug signal, investigate (AGENTS.md).**"
            if rs.n and rs.exp_r > 0
            else "."
        )
    )
    if T.height:
        a(
            f"Treatment geometry: median stop {float((T['r_unit'] / T['entry'] * 1e4).median()):.1f} bps, "  # type: ignore[arg-type]
            f"median target {float(T['target_r'].median()):.2f} R, minimum stop binding in "  # type: ignore[arg-type]
            f"{float(T['min_stop_bound'].mean()):.0%} of trades; exits: "  # type: ignore[arg-type]
            f"{dict(T['exit_kind'].value_counts().iter_rows())}."
        )
    a("")
    a(
        "Setups skipped before entry (counts per group/config): "
        + "; ".join(f"{s}: {sum(v.values())}" for s, v in meta["skips"].items())
        + " (details in meta.json)."
    )
    a("")
    n_tune, n_hold = up.n_sessions, rc.holdout_days * len(meta["symbols"])
    m = mde(up.se, n_tune, n_hold, rc.ci_level, rc.power) if np.isfinite(up.se) else float("nan")
    sd_t = float(T["net_r_default"].std()) if T.height > 1 else float("nan")  # type: ignore[arg-type]
    a("## 7. Hold-out MDE")
    a("")
    a(
        f"Paired uplift SE (session bootstrap) {up.se:.3f} R over {n_tune} sessions → MDE for "
        f"{n_hold} hold-out sessions = **{m:.3f} R** (gate +{rc.gate_uplift_r} R: "
        f"{'adequately powered' if m <= rc.gate_uplift_r else 'NOT adequately powered'}). Treatment "
        f"trades per session {st.n / max(n_tune, 1):.2f} → expected hold-out N ≈ {st.n / max(n_tune, 1) * n_hold:.0f}; "
        f"SD of net R {sd_t:.2f}."
    )
    out |= {
        "uplift": asdict(up),
        "mde": m,
        "weeks_positive_uplift": share,
        "random": asdict(rs),
        "treatment": asdict(st),
    }
    return "\n".join(L) + "\n", out
