"""Supply Zone fidelity check against the live signal log (candles only).

Each logged scan is replayed with the port at its logged time on Binance USD-M 1m klines.
No orderflow, no trade outcomes. The raw log stays outside the repository (wallet data).
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from flowlevels.config import SupplyZoneConfig
from flowlevels.data import klines
from flowlevels.strategies import supply_zone as sz

NEGATIVE = "no_strategy_signal"
NON_CORE_PORT = {
    "no_strategy_signal",
    "consolidation_regime",
    "insufficient_candles",
    "insufficient_data",
}


def log_positive(result: str, reason: str) -> bool | None:
    """True: the bot's core signal fired; False: it did not; None: not comparable (A12)."""
    if result in ("OPENED", "ORDER_FAILED", "SIGNAL"):
        return True
    if reason == NEGATIVE:
        return False
    if reason.startswith(("filter_", "insufficient_rr", "sl_calculation_failed")):
        return True
    if reason.startswith(("slot_busy", "low_rr_and_slot_busy", "max_positions_")):
        return True
    return None


@dataclass
class LogRow:
    instrument: str
    base: str
    symbol: str
    ts: int
    result: str
    reason: str
    regime: str
    direction: str
    entry: float
    sl: float
    confluence: float


def _f(x: str) -> float:
    try:
        return float(x)
    except ValueError:
        return float("nan")


def read_log(path: Path) -> list[LogRow]:
    out = []
    with path.open() as fh:
        for r in csv.DictReader(fh):
            base = r["instrument"].split("/")[0]
            ts = int(dt.datetime.fromisoformat(r["timestamp"]).timestamp() * 1000)
            out.append(
                LogRow(
                    r["instrument"], base, f"{base}USDT", ts, r["result"], r["reason"],
                    r["regime"], r["direction"], _f(r["entry_price"]), _f(r["sl_price"]),
                    _f(r["confluence_score"]),
                )
            )  # fmt: skip
    return out


def replay(rows: list[LogRow], root: Path, p: SupplyZoneConfig) -> pl.DataFrame:
    """Port decision at every logged scan whose symbol has klines."""
    recs: list[dict[str, Any]] = []
    for sym in sorted({r.symbol for r in rows}):
        rs = [r for r in rows if r.symbol == sym]
        t0, t1 = min(r.ts for r in rs), max(r.ts for r in rs)
        m = klines.load(root, sym, t0 - 40 * 86_400_000, t1 + 60_000)
        if m.height == 0:
            for r in rs:
                recs.append({**r.__dict__, "p_reason": "no_klines"})
            continue
        fr = sz.Frames(sz.Bars(*(m[c].to_numpy() for c in klines.COLS)))
        th = p.threshold(rs[0].base)
        for r in rs:
            sc = sz.scan(fr, r.ts, th, p)
            c = sc.core
            recs.append(
                {
                    **r.__dict__,
                    "p_regime": sc.regime,
                    "p_direction": sc.direction or "",
                    "p_reason": sc.reason,
                    "p_entry": c.entry if c else float("nan"),
                    "p_core_sl": c.sl if c else float("nan"),
                    "p_confluence": c.confluence if c else float("nan"),
                    "p_sl": sc.sl,
                }
            )
    return pl.DataFrame(recs, infer_schema_length=None)


def metrics(df: pl.DataFrame) -> dict[str, Any]:
    d = df.filter(pl.col("p_reason") != "no_klines")
    reg = d.filter(pl.col("regime").is_in(["BULL", "BEAR", "CONSOLIDATION"]))
    pos = [log_positive(a, b) for a, b in zip(d["result"], d["reason"], strict=True)]
    d = d.with_columns(pl.Series("log_pos", pos, dtype=pl.Boolean))
    core = d.filter(pl.col("log_pos").is_not_null()).with_columns(
        (~pl.col("p_reason").is_in(list(NON_CORE_PORT))).alias("port_pos")
    )
    lp = core.filter(pl.col("log_pos"))
    return {
        "symbols_checked": d["symbol"].n_unique(),
        "symbols_without_klines": sorted(set(df["symbol"]) - set(d["symbol"])),
        "regime_n": reg.height,
        "regime_agree": float((reg["regime"] == reg["p_regime"]).mean())  # type: ignore[arg-type]
        if reg.height
        else float("nan"),
        "core_n": core.height,
        "core_agree": float((core["log_pos"] == core["port_pos"]).mean())  # type: ignore[arg-type]
        if core.height
        else float("nan"),
        "core_agree_given_regime": float(
            core.filter(pl.col("regime") == pl.col("p_regime"))
            .select((pl.col("log_pos") == pl.col("port_pos")).mean())
            .item()
        )
        if core.height
        else float("nan"),
        "log_positives": lp.height,
        "positives_reproduced": int(lp["port_pos"].sum()) if lp.height else 0,
        "port_positives_on_log_negatives": int(core.filter(~pl.col("log_pos"))["port_pos"].sum()),
        "log_negatives": core.height - lp.height,
    }


def dump_frames(rows: list[tuple[str, int]], root: Path, p: SupplyZoneConfig, out: Path) -> None:
    """Frames for the reference (original pandas code) run: f1/f4 stacked with offsets."""
    f1s, f4s, meta = [], [], []
    cache: dict[str, sz.Frames] = {}
    for sym, ts in rows:
        if sym not in cache:
            m = klines.load(root, sym, 0, 2**62)
            cache[sym] = sz.Frames(sz.Bars(*(m[c].to_numpy() for c in klines.COLS)))
        f1, f4 = cache[sym].at(ts, p.n_1h, p.n_4h)
        f1s.append(np.c_[f1.ts, f1.o, f1.h, f1.lo, f1.c, f1.v])
        f4s.append(np.c_[f4.ts, f4.o, f4.h, f4.lo, f4.c, f4.v])
        base = sym[:-4]
        meta.append({"instrument": f"{base}/USDC:USDC", "symbol": sym, "ts": ts})
    off1 = np.r_[0, np.cumsum([len(x) for x in f1s])]
    off4 = np.r_[0, np.cumsum([len(x) for x in f4s])]
    np.savez(out, f1=np.vstack(f1s), f4=np.vstack(f4s), f1_off=off1, f4_off=off4,
             meta=json.dumps(meta))  # fmt: skip


def refcheck(npz: Path, ref_json: Path, p: SupplyZoneConfig) -> dict[str, Any]:
    """Port vs the original pandas code on identical frames."""
    d = np.load(npz, allow_pickle=True)
    meta = json.loads(str(d["meta"]))
    ref = json.loads(ref_json.read_text())

    def bars(pfx: str, k: int) -> sz.Bars:
        x = d[pfx][d[pfx + "_off"][k] : d[pfx + "_off"][k + 1]]
        return sz.Bars(x[:, 0].astype(np.int64), x[:, 1], x[:, 2], x[:, 3], x[:, 4], x[:, 5])

    n = agree_reason = agree_regime = 0
    num_bad = 0
    diffs: list[dict[str, Any]] = []
    for k, (m, r) in enumerate(zip(meta, ref, strict=True)):
        sc = sz.scan_frames(bars("f1", k), bars("f4", k), m["ts"], p.threshold(m["symbol"][:-4]), p)
        n += 1
        agree_regime += sc.regime == r["regime"]
        ok = sc.reason == r["reason"] or (
            sc.reason == "filter_rr_too_low" and str(r["reason"]).startswith("filter_rr_too_low")
        )
        agree_reason += ok
        if ok and sc.reason == "signal":
            for a, b in ((sc.sl, r.get("sl")), (sc.tp1, r.get("tp1")), (sc.tp2, r.get("tp2"))):
                if b is None or abs(a - b) > 1e-9 * max(1.0, abs(b)):
                    num_bad += 1
                    break
        if not ok and len(diffs) < 50:
            diffs.append(
                {"symbol": m["symbol"], "ts": m["ts"], "port": sc.reason, "ref": r["reason"],
                 "port_regime": sc.regime, "ref_regime": r["regime"]}
            )  # fmt: skip
    return {
        "n": n,
        "regime_agree": agree_regime / max(n, 1),
        "reason_agree": agree_reason / max(n, 1),
        "signal_level_mismatch": num_bad,
        "diffs": diffs,
    }
