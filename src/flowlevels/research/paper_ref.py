"""Paper-test reference backtest (DECISIONS PAPER-PLAN): the four paper variants on windows A
and B, six symbols, default costs + sensitivities. Only variant (c) `T2|wick|trail` is new
(cumulative configurations 77 -> 78); (a), (b), (d) are ITERATION-5 configurations recomputed
as a consistency check. Nothing is simulated or scored in the hold-out (window loader guards;
B's warm-up uses hold-out-span candles as indicator input only). Changes no verdict."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from flowlevels.config import AppConfig
from flowlevels.paper.variants import key_of, variant_grids
from flowlevels.research.iter4 import _monthly, _sens, session_se
from flowlevels.research.iter5 import load_window5
from flowlevels.research.plan import by_window, windows
from flowlevels.research.report import STATS_HEAD, _f, _stats_row, apply_costs
from flowlevels.strategies import method5 as m5

PRIOR_CONFIGS = 77
NEW_KEYS = ("T2|wick|trail",)


def ref_trades(cfg: AppConfig, kroot: Path) -> tuple[pl.DataFrame, dict[str, Any]]:
    gl = variant_grids(cfg)
    rows: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    for w, (start, end) in windows(cfg).items():
        for s in cfg.iteration5.symbols:
            m = load_window5(kroot, s, cfg, w)
            assert int(m["minute_ts"][-1]) < end
            r, mt = m5.run_symbol(m, start, end, cfg, s, gl)
            for x in r:
                x["window"] = w
            rows += r
            meta[f"{w} {s}"] = mt["skips"]
    return pl.DataFrame(rows, infer_schema_length=None), meta


def consistency(tr: pl.DataFrame, iter5_trades: Path | None) -> tuple[list[str], bool | None]:
    """Recomputed ITERATION-5 configurations must equal the stored ITERATION-5 run."""
    if iter5_trades is None or not iter5_trades.exists():
        return ["ITERATION-5 trades not available: consistency check skipped."], None
    old = pl.read_parquet(iter5_trades)
    key = ["window", "symbol", "config", "entry_ts"]
    L, ok = [], True
    for k in sorted(set(tr["config"].unique()) - set(NEW_KEYS)):
        a = old.filter(pl.col("config") == k)
        b = tr.filter(pl.col("config") == k).select(a.columns)
        same = a.sort(key).equals(b.sort(key), null_equal=True)
        ok &= same
        L.append(f"- `{k}`: {b.height} trades vs ITERATION-5 {a.height} → "
                 + ("**identical**" if same else "**DIFFERENT**"))  # fmt: skip
    return L, ok


def report(
    tr_raw: pl.DataFrame, meta: dict[str, Any], cfg: AppConfig, check: list[str]
) -> tuple[str, dict[str, Any]]:
    tr = apply_costs(tr_raw, cfg, only=tuple(cfg.plan.sensitivities))
    syms = cfg.iteration5.symbols
    L = ["## Paper variants on A and B (default costs, six symbols)", ""]
    L += ["| variant | key | N A | exp A | N B | exp B | N A+B | exp A+B R | bps | win | PF | max DD R | symbols > 0 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    out: dict[str, Any] = {"variants": {}}
    for v in cfg.paper.variants:
        k = key_of(v)
        x = tr.filter(pl.col("config") == k)
        st = by_window(x)
        ab = st["A+B"]
        npos = sum(
            1
            for s in syms
            if (g := by_window(x.filter(pl.col("symbol") == s))["A+B"]).n and g.exp_r > 0
        )
        L.append(
            f"| {v.id} | `{k}` | {st['A'].n} | {_f(st['A'].exp_r)} | {st['B'].n} | {_f(st['B'].exp_r)} | "
            f"{ab.n} | {_f(ab.exp_r)} | {ab.exp_bps:+.1f} | {ab.win_rate:.1%} | {ab.pf:.2f} | "
            f"{ab.max_dd_r:.1f} | {npos}/6 |"
        )
        out["variants"][v.id] = {
            "key": k, "n": ab.n, "exp_r": ab.exp_r, "exp_bps": ab.exp_bps,
            "win_rate": ab.win_rate, "a": st["A"].exp_r, "b": st["B"].exp_r,
        }  # fmt: skip
    L += (
        ["", "Consistency (recomputed ITERATION-5 configurations vs the stored run):", ""]
        + check
        + [""]
    )
    for k in NEW_KEYS:
        x = tr.filter(pl.col("config") == k)
        L += [f"### New variant `{k}` (reference run; no verdict changes)", "", STATS_HEAD]
        for w, gs in by_window(x).items():
            L.append(_stats_row(w, gs))
        L += ["", "Cost sensitivity (expectancy R):", ""] + _sens(x, cfg) + [""]
        L += ["Per symbol (A+B; A / B in the last column):", "",
              STATS_HEAD.split("\n")[0] + " A / B |", STATS_HEAD.split("\n")[1] + "---|"]  # fmt: skip
        for s in syms:
            sw = by_window(x.filter(pl.col("symbol") == s))
            L.append(_stats_row(s, sw["A+B"]) + f" {_f(sw['A'].exp_r)} / {_f(sw['B'].exp_r)} |")
        if x.height:
            kinds = dict(x.group_by("exit_kind").len().sort("exit_kind").iter_rows())
            L += ["", f"Exits {kinds}; median stop {float((x['r_unit'] / x['entry'] * 1e4).median()):.0f} bps "  # type: ignore[arg-type]
                  f"({float((x['r_unit'] / x['atr1h']).median()):.2f} × ATR(1h)) vs `T2|k0.5|trail` "  # type: ignore[arg-type]
                  f"{float(((y := tr.filter(pl.col('config') == 'T2|k0.5|trail'))['r_unit'] / y['entry'] * 1e4).median()):.0f} bps; "  # type: ignore[arg-type]
                  f"median TP1 {float(x['tp1_r'].median()):.2f} R."]  # type: ignore[arg-type]  # fmt: skip
            ml, share = _monthly(x)
            rc = cfg.research
            se = session_se(x, "default", rc.bootstrap_reps, rc.seed)
            L += ["", f"Monthly: {share:.0%} of months with net R > 0. Session-clustered SE "
                  f"{se:.3f} R → 90 % CI [{x['net_r_default'].mean() - 1.645 * se:+.3f}, "  # type: ignore[operator]
                  f"{x['net_r_default'].mean() + 1.645 * se:+.3f}] R.", ""] + ml  # type: ignore[operator]  # fmt: skip
        skips: dict[str, int] = {}
        for sk in meta.values():
            for r, n in sk.get(k, {}).items():
                skips[r] = skips.get(r, 0) + n
        L += [
            "",
            "Skips (A+B, all symbols): " + ", ".join(f"{r} {n}" for r, n in sorted(skips.items())),
            "",
        ]
    L.append(f"**Cumulative configurations tried in the in-sample windows: {PRIOR_CONFIGS} + "
             f"{len(NEW_KEYS)} = {PRIOR_CONFIGS + len(NEW_KEYS)}.** Hold-out runs: 0.")  # fmt: skip
    out["cumulative_configs"] = PRIOR_CONFIGS + len(NEW_KEYS)
    out["nan_check"] = (
        bool(np.isfinite(tr["net_r_default"].to_numpy()).all()) if tr.height else True
    )
    return "\n".join(L) + "\n", out
