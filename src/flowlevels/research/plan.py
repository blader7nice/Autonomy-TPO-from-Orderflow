"""MASTER-PLAN runners (DECISIONS "MASTER-PLAN"): windows A/B, Phase A (Supply Zone) and
Phase B (Dalton H1/H2), kill rules, reports. Candles from the kline store only.

Hold-out guard: candles are loaded per window as [start - warmup_days, end + 1 day). Window A
ends the day before the hold-out starts; B's warm-up lies in the hold-out's calendar span
and is used as indicator input only (as in Phase 3). Nothing is evaluated outside A or B.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from flowlevels.config import AppConfig
from flowlevels.data import klines
from flowlevels.research.report import STATS_HEAD, _f, _stats_row, apply_costs, stats_of
from flowlevels.research.stats import GroupStats
from flowlevels.strategies import dalton
from flowlevels.strategies import supply_zone as sz

DAY_MS = 86_400_000
SYMBOLS = ("BTCUSDT", "ETHUSDT")


def _ms(d: dt.date) -> int:
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.UTC).timestamp() * 1000)


def windows(cfg: AppConfig) -> dict[str, tuple[int, int]]:
    p = cfg.plan
    return {
        "A": (_ms(p.a_start), _ms(p.a_end) + DAY_MS),
        "B": (_ms(p.b_start), _ms(p.b_end) + DAY_MS),
    }


def load_window(root: Path, symbol: str, cfg: AppConfig, w: str) -> pl.DataFrame:
    start, end = windows(cfg)[w]
    hs, he = _ms(cfg.plan.holdout_start), _ms(cfg.plan.holdout_end) + DAY_MS
    if w == "A":
        assert end <= hs, "window A must end before the hold-out"
    if w == "B":
        assert start >= he, "window B must start after the hold-out"
    return klines.load(root, symbol, start - cfg.plan.warmup_days * DAY_MS, end)


# --------------------------------------------------------------------------- Phase A


def sz_trades(cfg: AppConfig, root: Path) -> tuple[pl.DataFrame, dict[str, Any]]:
    p = cfg.supply_zone
    rows: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"scans": {}, "reasons": {}}
    for w, (start, end) in windows(cfg).items():
        for s in SYMBOLS:
            m = load_window(root, s, cfg, w)
            fr = sz.Frames(sz.Bars(*(m[c].to_numpy() for c in klines.COLS)))
            trades, scans = sz.run_symbol(fr, s, start, end, p.threshold(s[:-4]), p)
            meta["scans"][f"{w} {s}"] = len(scans)
            rc: dict[str, int] = {}
            for x in scans:
                rc[x.reason] = rc.get(x.reason, 0) + 1
            meta["reasons"][f"{w} {s}"] = rc
            for t in trades:
                rows.append(
                    {
                        "window": w, "symbol": s, "direction": t.direction,
                        "entry_ts": t.entry_ts, "session": t.entry_ts // DAY_MS * DAY_MS,
                        "entry": t.entry, "exit_px": t.exit_px, "exit_ts": t.exit_ts,
                        "r_unit": t.r_unit, "sl": t.sl, "tp1": t.tp1, "tp2": t.tp2,
                        "x1": t.x1, "x2": t.x2, "kind1": t.kind1, "kind2": t.kind2,
                        "gross_r": t.direction * (t.exit_px - t.entry) / t.r_unit,
                        "mfe_r": float("nan"), "mae_r": float("nan"), "valid": True,
                        "window_end_close": t.window_end_close, "confluence": t.confluence,
                    }
                )  # fmt: skip
    schema_df = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    return schema_df, meta


# --------------------------------------------------------------------------- Phase B


def dalton_trades(cfg: AppConfig, root: Path) -> tuple[pl.DataFrame, dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"skips": {}, "sessions": {}}
    for w, (start, end) in windows(cfg).items():
        days = list(range(start, end, DAY_MS))
        for s in SYMBOLS:
            m = load_window(root, s, cfg, w)
            r, sk = dalton.run_symbol(m, days, cfg, s)
            for x in r:
                x["window"] = w
            rows += r
            meta["skips"][f"{w} {s}"] = sk
            meta["sessions"][f"{w} {s}"] = len(dalton.sessions(m, days, cfg, s))
    return (pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()), meta


# --------------------------------------------------------------------------- stats


def by_window(df: pl.DataFrame, scen: str = "default") -> dict[str, GroupStats]:
    out = {}
    for w in ("A", "B", "A+B"):
        x = df if w == "A+B" else df.filter(pl.col("window") == w)
        out[w] = stats_of(x, scen)
    return out


def survives(st: dict[str, GroupStats]) -> tuple[bool, str]:
    a, b, ab = st["A"], st["B"], st["A+B"]
    if ab.n == 0 or a.n == 0 or b.n == 0:
        return False, "no trades in A or B (sign undefined)"
    if not ab.exp_r > 0:
        return False, f"A+B net expectancy {ab.exp_r:+.3f} R <= 0"
    if np.sign(a.exp_r) != np.sign(b.exp_r):
        return False, f"sign differs: A {a.exp_r:+.3f} R vs B {b.exp_r:+.3f} R"
    return True, f"A+B {ab.exp_r:+.3f} R > 0, same sign in A and B"


def _window_table(df: pl.DataFrame, cfg: AppConfig) -> list[str]:
    L = [STATS_HEAD]
    for w, s in by_window(df).items():
        L.append(_stats_row(w, s))
    L.append("")
    L.append("Cost sensitivity (expectancy R per window; the trades are identical):")
    L.append("")
    L.append("| scenario | A | B | A+B | A+B bps |")
    L.append("|---|---|---|---|---|")
    for sc in cfg.plan.sensitivities:
        st = by_window(df, sc)
        L.append(
            f"| {sc} | {_f(st['A'].exp_r)} | {_f(st['B'].exp_r)} | {_f(st['A+B'].exp_r)} | "
            f"{st['A+B'].exp_bps:+.1f} |"
        )
    return L


def phase_a_report(
    tr_raw: pl.DataFrame, meta: dict[str, Any], cfg: AppConfig
) -> tuple[str, dict[str, Any]]:
    L = ["## Phase A — ITERATION-2: Supply Zone bot (1 configuration)", ""]
    if tr_raw.height == 0:
        L.append("No trades in A or B → **Phase A: KILLED** (no trades).")
        return "\n".join(L) + "\n", {"verdict": "KILLED", "reason": "no trades"}
    tr = apply_costs(tr_raw, cfg, only=tuple(cfg.plan.sensitivities))
    L += _window_table(tr, cfg)
    st = by_window(tr)
    ok, why = survives(st)
    L += [
        "",
        f"Kill rule (net ≤ 0 R on A+B, or sign(A) ≠ sign(B)): **{'SURVIVES' if ok else 'KILLED'}** — {why}.",
        "",
    ]
    L.append("Breakdown (A+B, default costs):")
    L.append("")
    L.append(STATS_HEAD)
    for name, flt in (
        ("BTCUSDT", pl.col("symbol") == "BTCUSDT"), ("ETHUSDT", pl.col("symbol") == "ETHUSDT"),
        ("long", pl.col("direction") == 1), ("short", pl.col("direction") == -1),
    ):  # fmt: skip
        L.append(_stats_row(name, stats_of(tr.filter(flt))))
    L.append("")
    kinds = tr.group_by("kind1", "kind2").len().sort("len", descending=True)
    L.append(
        "Exit combinations (TP1 leg / TP2 leg): "
        + ", ".join(f"{a}/{b}: {n}" for a, b, n in kinds.iter_rows())
    )
    L.append(
        f"Median initial stop {float((tr['r_unit'] / tr['entry'] * 1e4).median()):.0f} bps; "  # type: ignore[arg-type]
        f"trades closed at a window end: {int(tr['window_end_close'].sum())}; hourly scans {meta['scans']}."
    )
    L.append("")
    L.append("Scan outcomes per window/symbol: " + "; ".join(
        f"{k}: " + ", ".join(f"{r} {n}" for r, n in sorted(v.items(), key=lambda x: -x[1])[:8])
        for k, v in meta["reasons"].items()
    ))  # fmt: skip
    return "\n".join(L) + "\n", {
        "verdict": "SURVIVES" if ok else "KILLED", "reason": why,
        "stats": {w: s.__dict__ for w, s in st.items()},
    }  # fmt: skip


def phase_b_report(
    tr_raw: pl.DataFrame, meta: dict[str, Any], cfg: AppConfig
) -> tuple[str, dict[str, Any]]:
    L = ["## Phase B — ITERATION-3: Dalton Market Profile (H1, H2)", ""]
    out: dict[str, Any] = {"n_configs": len(dalton.grids()), "rules": {}}
    if tr_raw.height == 0:
        L.append("No trades → both hypotheses KILLED.")
        out["rules"] = {"H1": {"verdict": "KILLED"}, "H2": {"verdict": "KILLED"}}
        return "\n".join(L) + "\n", out
    tr = apply_costs(tr_raw, cfg, only=tuple(cfg.plan.sensitivities))
    prim = tr.filter(pl.col("exit") == "target")
    L.append(f"Sessions with a prior-day VA: {meta['sessions']}. Primary exit: opposite VA edge.")
    L.append("")
    L.append(
        "| config | N A | exp A | N B | exp B | N A+B | exp A+B R | bps | win | PF | max DD R | eligible |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    grid_rows = []
    for g in dalton.grids():
        x = prim.filter(pl.col("config") == g.key)
        st = by_window(x)
        grid_rows.append((g, st))
        ab = st["A+B"]
        L.append(
            f"| {g.key} | {st['A'].n} | {_f(st['A'].exp_r)} | {st['B'].n} | {_f(st['B'].exp_r)} | "
            f"{ab.n} | {_f(ab.exp_r)} | {ab.exp_bps:+.1f} | {ab.win_rate:.1%} | {ab.pf:.2f} | "
            f"{ab.max_dd_r:.1f} | {'yes' if ab.n >= cfg.plan.min_trades else 'n<' + str(cfg.plan.min_trades)} |"
        )
    L.append("")
    for rule in ("H1", "H2"):
        cand = [
            (g, st) for g, st in grid_rows if g.rule == rule and st["A+B"].n >= cfg.plan.min_trades
        ]
        if not cand:
            L.append(
                f"**{rule}: KILLED** — no configuration with ≥ {cfg.plan.min_trades} trades on A+B."
            )
            out["rules"][rule] = {"verdict": "KILLED", "reason": "no config with enough trades"}
            continue
        g, st = sorted(cand, key=lambda c: (-c[1]["A+B"].exp_r, c[1]["A+B"].max_dd_r))[0]
        ok, why = survives(st)
        L.append(f"**{rule}: selected `{g.key}` → {'SURVIVES' if ok else 'KILLED'}** — {why}.")
        out["rules"][rule] = {"verdict": "SURVIVES" if ok else "KILLED", "reason": why, "selected": g.key,
                              "stats": {w: s.__dict__ for w, s in st.items()}}  # fmt: skip
        x = prim.filter(pl.col("config") == g.key)
        L.append("")
        L += _window_table(x, cfg)
        kinds = x.group_by("exit_kind").len().sort("len", descending=True)
        L.append("")
        L.append(
            f"Exits: {dict(kinds.iter_rows())}; median stop "
            f"{float((x['r_unit'] / x['entry'] * 1e4).median()):.0f} bps; median target "  # type: ignore[arg-type]
            f"{float(x['target_r'].median()):.2f} R."  # type: ignore[arg-type]
        )
        if rule == "H2":
            pc = tr.filter((pl.col("config") == g.key) & (pl.col("exit") == "poc"))
            ps = by_window(pc)
            L.append(
                f"Secondary exit POC (same trades where the POC lies ahead): N {ps['A+B'].n}, "
                f"A {_f(ps['A'].exp_r)}, B {_f(ps['B'].exp_r)}, A+B {_f(ps['A+B'].exp_r)} R."
            )
        L.append("")
    return "\n".join(L) + "\n", out
