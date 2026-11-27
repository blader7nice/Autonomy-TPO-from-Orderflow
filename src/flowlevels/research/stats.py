"""Group metrics, paired uplift with a session-clustered bootstrap CI, and the MDE.

Touches on the same day share market conditions, so trades are not independent. The
bootstrap resamples **sessions** (symbol, UTC day) with replacement (fixed seed), which
keeps that correlation; uplift = mean net R of treatment - mean net R of unconfirmed over
the resampled sessions (D-036).

MDE for the hold-out (SPEC 10, Phase 3b): the bootstrap SE of the uplift in the tuning
window, scaled by sqrt(sessions_tuning / sessions_holdout) (the per-session event rate is
assumed equal), times z_(1 - alpha) + z_power with alpha = (1 - ci_level) / 2 one-sided,
matching the gate "lower bound of the 90% CI > 0".
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import NormalDist

import numpy as np


@dataclass(frozen=True)
class GroupStats:
    n: int
    win_rate: float
    exp_r: float
    exp_bps: float
    pf: float
    mfe_r: float
    mae_r: float
    max_dd_r: float
    cost_r: float
    q: tuple[float, float, float, float, float]  # 5/25/50/75/95 % of net R


def group_stats(
    net_r: np.ndarray,
    bps: np.ndarray,
    gross_r: np.ndarray,
    mfe: np.ndarray,
    mae: np.ndarray,
    order: np.ndarray,
) -> GroupStats:
    n = len(net_r)
    if n == 0:
        nan = float("nan")
        return GroupStats(0, nan, nan, nan, nan, nan, nan, nan, nan, (nan, nan, nan, nan, nan))
    pos, neg = net_r[net_r > 0].sum(), -net_r[net_r < 0].sum()
    eq = np.cumsum(net_r[np.argsort(order, kind="stable")])
    dd = float((np.maximum.accumulate(np.r_[0.0, eq]) - np.r_[0.0, eq]).max())
    q = np.percentile(net_r, [5, 25, 50, 75, 95])
    return GroupStats(
        n,
        float((net_r > 0).mean()),
        float(net_r.mean()),
        float(bps.mean()),
        float(pos / neg) if neg > 0 else float("inf"),
        float(mfe.mean()),
        float(mae.mean()),
        dd,
        float((gross_r - net_r).mean()),
        (float(q[0]), float(q[1]), float(q[2]), float(q[3]), float(q[4])),
    )


@dataclass(frozen=True)
class Uplift:
    n_t: int
    n_u: int
    mean_t: float
    mean_u: float
    uplift: float
    lo: float
    hi: float
    se: float
    n_sessions: int


def paired_uplift(
    r_t: np.ndarray,
    s_t: np.ndarray,
    r_u: np.ndarray,
    s_u: np.ndarray,
    reps: int,
    ci: float,
    seed: int,
) -> Uplift:
    """Uplift with a session-clustered bootstrap. s_*: session keys (int) per trade."""
    nan = float("nan")
    if len(r_t) == 0 or len(r_u) == 0:
        return Uplift(len(r_t), len(r_u), nan, nan, nan, nan, nan, nan, 0)
    keys = np.unique(np.r_[s_t, s_u])
    it, iu = np.searchsorted(keys, s_t), np.searchsorted(keys, s_u)
    k = len(keys)
    st, nt = np.bincount(it, r_t, k), np.bincount(it, minlength=k).astype(float)
    su, nu = np.bincount(iu, r_u, k), np.bincount(iu, minlength=k).astype(float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, k, size=(reps, k))
    with np.errstate(invalid="ignore", divide="ignore"):
        bt = st[idx].sum(1) / nt[idx].sum(1)
        bu = su[idx].sum(1) / nu[idx].sum(1)
    d = bt - bu
    d = d[np.isfinite(d)]
    a = (1 - ci) / 2
    mt, mu = float(r_t.mean()), float(r_u.mean())
    return Uplift(
        len(r_t),
        len(r_u),
        mt,
        mu,
        mt - mu,
        float(np.quantile(d, a)),
        float(np.quantile(d, 1 - a)),
        float(d.std(ddof=1)),
        k,
    )


def mde(
    se_tuning: float, sessions_tuning: int, sessions_holdout: int, ci: float, power: float
) -> float:
    z = NormalDist().inv_cdf
    se_h = se_tuning * np.sqrt(sessions_tuning / sessions_holdout)
    return float((z(1 - (1 - ci) / 2) + z(power)) * se_h)


def weekly_positive_share(
    r_t: np.ndarray, w_t: np.ndarray, r_u: np.ndarray, w_u: np.ndarray
) -> tuple[float, int]:
    """Share of weeks (with both groups present) where mean(t) - mean(u) > 0, and #weeks."""
    weeks = sorted(set(w_t.tolist()) & set(w_u.tolist()))
    if not weeks:
        return float("nan"), 0
    pos = sum(r_t[w_t == w].mean() > r_u[w_u == w].mean() for w in weeks)
    return pos / len(weeks), len(weeks)
