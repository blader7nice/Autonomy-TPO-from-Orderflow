"""Paper performance statistics (docs/PAPER_TEST.md): per-variant ledger stats, the
session-clustered bootstrap CI, equity, drawdown and weekly table."""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np

DAY = 86_400_000
BOOT_REPS = 10_000
BOOT_SEED = 42
Z90 = 1.645


def session_ci(trades: list[dict[str, Any]]) -> tuple[float, float, float] | None:
    """(mean, lo, hi) 90 % CI of the mean net R; SE by bootstrap over sessions
    (symbol x UTC entry day), as research `session_se`."""
    if len(trades) < 2:
        return None
    groups: dict[tuple[str, int], list[float]] = {}
    for t in trades:
        groups.setdefault((t["symbol"], int(t["entry_ts"]) // DAY), []).append(float(t["net_r"]))
    s = np.array([sum(v) for v in groups.values()])
    n = np.array([len(v) for v in groups.values()], dtype=float)
    mean = float(s.sum() / n.sum())
    if len(s) < 2:
        return None
    rng = np.random.default_rng(BOOT_SEED)
    idx = rng.integers(0, len(s), size=(BOOT_REPS, len(s)))
    se = float((s[idx].sum(1) / n[idx].sum(1)).std(ddof=1))
    return mean, mean - Z90 * se, mean + Z90 * se


def max_drawdown(r: list[float]) -> float:
    if not r:
        return 0.0
    eq = np.cumsum(r)
    peak = np.maximum.accumulate(np.r_[0.0, eq])[1:]
    return float((peak - eq).max())


def iso_week(ts: int) -> str:
    d = dt.datetime.fromtimestamp(ts / 1000, dt.UTC)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def variant_stats(
    trades: list[dict[str, Any]], ci_min: int, freeze: int, decision: int, ci_lower_min: float
) -> dict[str, Any]:
    """Stats of one variant ledger (trades of its active version)."""
    closed = sorted((t for t in trades if t["status"] == "closed"), key=lambda t: t["exit_ts"])
    opened = [t for t in trades if t["status"] == "open"]
    r = [float(t["net_r"]) for t in closed]
    bps = [float(t["net_bps"]) for t in closed]
    n = len(closed)
    wins = sum(1 for x in r if x > 0)
    ci = session_ci(closed) if n >= ci_min else None
    eq = list(np.cumsum(r)) if r else []
    weekly: dict[str, dict[str, float]] = {}
    for t in closed:
        w = weekly.setdefault(iso_week(int(t["entry_ts"])), {"n": 0, "sum_r": 0.0, "wins": 0})
        w["n"] += 1
        w["sum_r"] += float(t["net_r"])
        w["wins"] += float(t["net_r"]) > 0
    exp_r = float(np.mean(r)) if r else None
    rule_ok = (
        n >= decision
        and exp_r is not None
        and exp_r > 0
        and ci is not None
        and ci[1] > ci_lower_min
    )
    return {
        "n_closed": n,
        "n_open": len(opened),
        "n_late": sum(1 for t in trades if t.get("detected_late")),
        "wins": wins,
        "win_rate": wins / n if n else None,
        "exp_r": exp_r,
        "exp_bps": float(np.mean(bps)) if bps else None,
        "sum_r": float(sum(r)),
        "unreal_r": float(sum(float(t["net_r"] or 0) for t in opened)),
        "max_dd_r": max_drawdown(r),
        "avg_win_r": float(np.mean([x for x in r if x > 0])) if wins else None,
        "avg_loss_r": float(np.mean([x for x in r if x <= 0])) if n - wins else None,
        "ci": {"mean": ci[0], "lo": ci[1], "hi": ci[2]} if ci else None,
        "equity": [[int(t["exit_ts"]) // 1000, float(e)] for t, e in zip(closed, eq, strict=True)],
        "weekly": [{"week": k, **v} for k, v in sorted(weekly.items())],
        "progress": {
            "freeze": freeze,
            "decision": decision,
            "frozen": n < freeze,
            "decision_rule_met": bool(rule_ok),
        },
    }
