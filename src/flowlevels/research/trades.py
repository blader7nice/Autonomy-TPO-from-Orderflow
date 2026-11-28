"""Trade model (SPEC 9): entry at a bar close (= next bar open), stop, exits on the 1m path.

- Entry price = open of the 1m row starting at entry_ts (the first minute of the next bar).
  If that minute has no row, the trade is skipped ("no_entry_minute").
- R unit = |entry - stop| before costs; a stop closer than min_stop is moved out to min_stop.
  A stop on the wrong side of the entry (already hit) makes the trade invalid.
- Exits, simulated separately and all with the time stop at max_hold_minutes: target at
  k R (k in targets_r), the next static zone in the trade direction ("zone"; time stop only
  if there is none) and the time stop alone ("time"). Minute by minute from the entry minute:
  if the stop is touched the trade stops out, even when the target is touched in the same
  minute (the conservative rule); a target exit fills at the target, a stop at the stop, a
  time exit at the close of the last minute.
- Costs, per unit of base asset: fill = price -/+ slippage on both sides (adverse), fee =
  fee_rate x fill price on both sides. net = direction x (exit_fill - entry_fill) - fees.
  net_R = net / R unit; bps = net / entry price x 1e4; cost_R = gross_R - net_R.
  The execution venue is OKX inverse swaps; valued at the exit price the PnL equals that of a
  linear contract of the same USD notional, so R and bps are unaffected (D-033).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MINUTE_MS = 60_000


@dataclass(frozen=True)
class Path1m:
    ts: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray

    def index(self, t_ms: int) -> int | None:
        i = int(np.searchsorted(self.ts, t_ms))
        if i < len(self.ts) and self.ts[i] == t_ms:
            return i
        return None


@dataclass(frozen=True)
class Costs:
    slip: float  # price units per side
    entry_fee: float  # fraction of notional
    exit_fee: float
    entry_slip: bool = True  # False for the maker-entry sensitivity


@dataclass
class Fill:
    valid: bool
    reason: str = ""
    entry: float = float("nan")
    stop: float = float("nan")
    r_unit: float = float("nan")
    exit_ts: int = 0
    exit_px: float = float("nan")
    exit_kind: str = ""
    gross_r: float = float("nan")
    net_r: float = float("nan")
    net_bps: float = float("nan")
    mfe_r: float = float("nan")
    mae_r: float = float("nan")


def plan_stop(entry: float, stop: float, direction: int, min_stop: float) -> tuple[float, float]:
    """(stop, r_unit) after the minimum distance; r_unit <= 0 means invalid."""
    dist = direction * (entry - stop)
    if dist <= 0:
        return stop, dist
    if dist < min_stop:
        return entry - direction * min_stop, min_stop
    return stop, dist


def simulate(
    path: Path1m,
    entry_ts: int,
    direction: int,
    stop: float,
    min_stop: float,
    exit_name: str,
    next_zone: float | None,
    max_hold_minutes: int,
    costs: Costs,
) -> Fill:
    i0 = path.index(entry_ts)
    if i0 is None:
        return Fill(False, "no_entry_minute")
    entry = float(path.open[i0])
    stop, r = plan_stop(entry, stop, direction, min_stop)
    if r <= 0:
        return Fill(False, "stop_beyond_entry", entry, stop)
    target = exit_target(exit_name, entry, r, direction, next_zone)
    end_ts = entry_ts + max_hold_minutes * MINUTE_MS
    i1 = int(np.searchsorted(path.ts, end_ts))  # minutes [i0, i1)
    hi, lo = path.high[i0:i1], path.low[i0:i1]
    if direction > 0:
        stop_hit = lo <= stop
        tgt_hit = hi >= target if target is not None else np.zeros(len(hi), bool)
    else:
        stop_hit = hi >= stop
        tgt_hit = lo <= target if target is not None else np.zeros(len(hi), bool)
    ks = int(np.argmax(stop_hit)) if stop_hit.any() else len(hi)
    kt = int(np.argmax(tgt_hit)) if tgt_hit.any() else len(hi)
    if ks <= kt and ks < len(hi):
        k, px, kind = ks, stop, "stop"
    elif kt < len(hi):
        k, px, kind = kt, float(target), "target"  # type: ignore[arg-type]
    else:
        k, px, kind = len(hi) - 1, float(path.close[i1 - 1]), "time"
    seg_hi, seg_lo = hi[: k + 1], lo[: k + 1]
    if direction > 0:
        mfe, mae = float(seg_hi.max()) - entry, entry - float(seg_lo.min())
    else:
        mfe, mae = entry - float(seg_lo.min()), float(seg_hi.max()) - entry
    if kind == "stop":
        mae = min(mae, r)  # the stop caps the adverse excursion that counts
    e_fill = entry + direction * (costs.slip if costs.entry_slip else 0.0)
    x_fill = px - direction * costs.slip
    net = direction * (x_fill - e_fill) - costs.entry_fee * e_fill - costs.exit_fee * x_fill
    gross = direction * (px - entry)
    return Fill(
        True,
        "",
        entry,
        stop,
        r,
        int(path.ts[i0 + k]),
        px,
        kind,
        gross / r,
        net / r,
        net / entry * 1e4,
        mfe / r,
        mae / r,
    )


def exit_target(
    exit_name: str, entry: float, r_unit: float, direction: int, next_zone: float | None
) -> float | None:
    """Target price for an exit variant ("1R".."3R", "zone", "time"); None = time stop only."""
    if exit_name == "time":
        return None
    if exit_name == "zone":
        if next_zone is None or direction * (next_zone - entry) <= 0:
            return None
        return next_zone
    return entry + direction * float(exit_name.rstrip("R")) * r_unit
