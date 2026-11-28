"""ITERATION-1 (DECISIONS, pre-registered 2026-11-27): level-to-level trades.

Orderflow confirmation is used as entry timing; the trade runs from the touched static zone
to the nearest other static zone in the trade direction. Same data, levels, touches,
detectors and trade simulator as the Phase 3 run (one code path); only the event rules
differ:

- setups: first touch (touch_n = 1) of a static zone whose score >= the frozen median;
- decision bar: treatment = the window bar where the score reaches the threshold (W bars),
  unconfirmed = bar W-1 when it never does, baseline = the touch bar, random = a random
  bar inside a qualifying zone (random direction, count and hour matched to the setups);
- entry: next 5m bar open; stop: beyond the decision bar's extreme + buffer, at least
  max(k x ATR(1h) as of entry, min_stop_cost_mult x round-trip cost);
- target: the near edge of the nearest other static zone beyond the entry; the trade is
  taken only if target distance >= min_target_r x stop distance;
- exits: "zone" (primary) plus the secondary fixed-R exits, all with the time stop.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

import numpy as np
import polars as pl

from flowlevels.config import AppConfig
from flowlevels.data.quality import Exclusions
from flowlevels.data.storage import ParquetStore
from flowlevels.footprint.bars import res_ms
from flowlevels.levels.engine import DAY_MS, HOUR_MS
from flowlevels.research.backtest import (
    SymbolData,
    load_symbol,
    prepare_res,
    signal_bar,
    tuning_days,
)
from flowlevels.research.touches import SessionZones, _assign
from flowlevels.research.trades import Costs, plan_stop, simulate

log = logging.getLogger(__name__)

PRIMARY_EXIT = "zone"


def hourly_atr(oh: pl.DataFrame, period: int) -> tuple[np.ndarray, np.ndarray]:
    """(known_ts, atr): ATR(1h) = SMA(period) of true range of complete hours, known at h+1h."""
    h = (
        oh.with_columns((pl.col("minute_ts") // HOUR_MS * HOUR_MS).alias("h"))
        .group_by("h")
        .agg(pl.col("high").max(), pl.col("low").min(), pl.col("close").last())
        .sort("h")
    )
    pc = pl.col("close").shift(1)
    tr = pl.max_horizontal(
        pl.col("high") - pl.col("low"), (pl.col("high") - pc).abs(), (pl.col("low") - pc).abs()
    )
    h = h.with_columns(tr.alias("tr")).with_columns(
        pl.when(pc.is_not_null()).then(pl.col("tr")).alias("tr")
    )
    h = h.with_columns(pl.col("tr").rolling_mean(period).alias("atr")).drop_nulls("atr")
    return (h["h"].to_numpy() + HOUR_MS), h["atr"].to_numpy()


def zone_score_median(sessions: list[dict[int, SessionZones]]) -> float:
    scores = [z.score for ss in sessions for sz in ss.values() for z in sz.zones]
    return float(np.median(scores))


def round_trip_cost(cfg: AppConfig, symbol: str, entry: float) -> float:
    """Default-scenario round trip in price units: 2 x taker fee x entry + 2 x slippage."""
    ss = cfg.costs.slippage_scenarios[cfg.costs.slippage]
    unit = (
        cfg.symbol(symbol).bucket
        if ss.unit == "bucket"
        else cfg.costs.execution_instruments[symbol].tick
    )
    return 2 * cfg.costs.taker_fee * entry + 2 * ss.amount * unit


def target_zone(sz: SessionZones, entry: float, direction: int, exclude: str) -> float | None:
    """Near edge of the nearest other static zone entirely beyond the entry."""
    if direction > 0:
        c = [z.low for z in sz.zones if z.zone_id != exclude and z.low > entry]
        return min(c) if c else None
    c = [z.high for z in sz.zones if z.zone_id != exclude and z.high < entry]
    return max(c) if c else None


class Trader:
    """Builds and simulates ITERATION-1 trades for one symbol (cached per unique trade)."""

    def __init__(self, sd: SymbolData, cfg: AppConfig, bars: pl.DataFrame) -> None:
        self.sd, self.cfg = sd, cfg
        it = cfg.iteration1
        self.step = res_ms(it.resolution)
        self.lo, self.hi = bars["low"].to_numpy(), bars["high"].to_numpy()
        self.bts = bars["bar_ts"].to_numpy()
        self.buf = (
            cfg.trade_model.stop_buffer_ticks * cfg.costs.execution_instruments[sd.symbol].tick
        )
        self.atr_ts, self.atr = hourly_atr(sd.oh, cfg.levels.atr_period_1h)
        self.max_hold = it.time_stop_bars * self.step // 60_000
        self.exits = [PRIMARY_EXIT, *it.secondary_exits]
        self.cache: dict[tuple[int, int, float, str], list[dict[str, Any]]] = {}

    def trade(
        self, p: int, direction: int, k_atr: float, session: int, zone_id: str
    ) -> list[dict[str, Any]] | str:
        """Trades (one per exit) for decision bar p, or a skip reason."""
        if p + 1 >= len(self.bts):
            return "beyond_data"
        entry_ts = int(self.bts[p]) + self.step
        key = (entry_ts, direction, k_atr, zone_id)
        if key in self.cache:
            return self.cache[key]
        path = self.sd.path
        i0 = path.index(entry_ts)
        if i0 is None:
            return "no_entry_minute"
        entry = float(path.open[i0])
        j = int(np.searchsorted(self.atr_ts, entry_ts, side="right")) - 1
        if j < 0:
            return "no_atr"
        min_stop = max(
            k_atr * float(self.atr[j]),
            self.cfg.iteration1.min_stop_cost_mult
            * round_trip_cost(self.cfg, self.sd.symbol, entry),
        )
        raw_stop = self.lo[p] - self.buf if direction > 0 else self.hi[p] + self.buf
        _, r = plan_stop(entry, raw_stop, direction, min_stop)
        if r <= 0:
            return "stop_beyond_entry"
        tgt = target_zone(self.sd.sessions[session], entry, direction, zone_id)
        if tgt is None:
            return "no_target_zone"
        if direction * (tgt - entry) < self.cfg.iteration1.min_target_r * r:
            return "target_too_close"
        out = []
        for e in self.exits:
            f = simulate(
                path, entry_ts, direction, raw_stop, min_stop, e, tgt, self.max_hold, Costs(0, 0, 0)
            )
            out.append(
                {
                    "exit": e,
                    "valid": f.valid,
                    "entry_ts": entry_ts,
                    "entry": f.entry,
                    "stop": f.stop,
                    "r_unit": f.r_unit,
                    "target": tgt,
                    "target_r": direction * (tgt - entry) / r,
                    "exit_ts": f.exit_ts,
                    "exit_px": f.exit_px,
                    "exit_kind": f.exit_kind,
                    "gross_r": f.gross_r,
                    "mfe_r": f.mfe_r,
                    "mae_r": f.mae_r,
                    "min_stop_bound": min_stop >= -direction * (raw_stop - entry),
                }
            )
        self.cache[key] = out
        return out


def _setups(touches: pl.DataFrame, cfg: AppConfig, min_score: float) -> pl.DataFrame:
    x = touches.with_row_index("tidx").filter(pl.col("pool") == "static")
    if cfg.iteration1.first_touch_only:
        x = x.filter(pl.col("touch_n") == 1)
    return x.filter(pl.col("score") >= min_score - 1e-12)


def random_setups(
    sd: SymbolData, bars: pl.DataFrame, setups: pl.DataFrame, min_score: float, seed: int
) -> list[dict[str, Any]]:
    """Random bars inside qualifying static zones, hour-matched to the setups."""
    ts = bars["bar_ts"].to_numpy()
    lo, hi, cl = (bars[c].to_numpy() for c in ("low", "high", "close"))
    prev_c = np.r_[np.nan, cl[:-1]]
    day = ts // DAY_MS * DAY_MS
    cand: list[tuple[int, str, int]] = []
    for d0, sz in sd.sessions.items():
        zones = [z for z in sz.zones if z.score >= min_score - 1e-12]
        idx = np.flatnonzero((day == d0) & np.isfinite(prev_c))
        if not zones or not len(idx):
            continue
        anc = np.array([next(m.price for m in z.members if m.name == z.anchor) for z in zones])
        a = _assign(
            lo[idx],
            hi[idx],
            prev_c[idx],
            np.array([z.low for z in zones]),
            np.array([z.high for z in zones]),
            anc,
            np.array([z.score for z in zones]),
        )
        cand += [(int(p), zones[int(k)].zone_id, d0) for p, k in zip(idx, a, strict=True) if k >= 0]
    if not cand or setups.height == 0:
        return []
    rng = np.random.default_rng(seed)
    by_hour: dict[int, list[int]] = {}
    for j, (p, _, _) in enumerate(cand):
        by_hour.setdefault(int((ts[p] % DAY_MS) // HOUR_MS), []).append(j)
    out = []
    for h in (setups["bar_ts"].to_numpy() % DAY_MS) // HOUR_MS:
        pool = by_hour.get(int(h))
        if not pool:
            continue
        p, zid, d0 = cand[pool[int(rng.integers(len(pool)))]]
        out.append(
            {
                "pos": p,
                "zone_id": zid,
                "session": d0,
                "direction": int(rng.choice([-1, 1])),
                "bar_ts": int(ts[p]),
            }
        )
    return out


def run(
    store: ParquetStore, cfg: AppConfig, symbols: list[str], min_score: float | None = None
) -> tuple[pl.DataFrame, dict[str, Any]]:
    t0 = time.perf_counter()
    it = cfg.iteration1
    days = tuning_days(store, symbols, cfg.research.tuning_days)
    excl = Exclusions(holes=[])
    # The detector masks need the largest window of this grid.
    cfg_r = cfg.model_copy(
        update={
            "research": cfg.research.model_copy(update={"grid_window_bars": it.grid_window_bars}),
            "signals": cfg.signals.model_copy(update={"resolutions": [it.resolution]}),
        }
    )
    sds = {s: load_symbol(store, cfg_r, s, days, excl) for s in symbols}
    med = zone_score_median([sd.sessions for sd in sds.values()])
    score_min = it.min_zone_score if it.min_zone_score is not None else (min_score or med)
    meta: dict[str, Any] = {
        "days": [days[0].isoformat(), days[-1].isoformat()],
        "n_days": len(days),
        "sessions": {s: len(sd.sessions) for s, sd in sds.items()},
        "zone_score_median": med,
        "min_zone_score_used": score_min,
        "skips": {},
        "setups": {},
    }
    rows: list[dict[str, Any]] = []
    weights = cfg.signals.weights
    for si, (s, sd) in enumerate(sds.items()):
        rd = prepare_res(sd, cfg_r, it.resolution)
        setups = _setups(rd.touches, cfg, score_min)
        masks = rd.masks[setups["tidx"].to_numpy()]
        meta["setups"][s] = setups.height
        tr = Trader(sd, cfg, rd.bars)
        skips: dict[str, int] = {}

        def add(
            group: str,
            info: dict[str, Any],
            p: int,
            k: float,
            extra: dict[str, Any],
            tr: Trader = tr,
            skips: dict[str, int] = skips,
            s: str = s,
        ) -> None:
            res = tr.trade(p, info["direction"], k, info["session"], info["zone_id"])
            if isinstance(res, str):
                key = f"{group} {extra.get('thr', '')}/{extra.get('w', '')}/{k} {res}"
                skips[key] = skips.get(key, 0) + 1
                return
            for r in res:
                rows.append({"symbol": s, "group": group, "k_atr": k, **extra, **info, **r})

        st = setups.to_dicts()
        for thr in it.grid_score_threshold:
            for w in it.grid_window_bars:
                sig = signal_bar(masks, w, thr, weights)
                cum = np.bitwise_or.accumulate(masks[:, :w], axis=1)
                for k in it.grid_min_stop_k_atr:
                    for i, t in enumerate(st):
                        info = {
                            "touch": int(t["tidx"]),
                            "session": int(t["session"]),
                            "zone_id": t["zone_id"],
                            "direction": int(t["direction"]),
                            "touch_ts": int(t["bar_ts"]),
                            "anchor_type": t["anchor_type"],
                            "score": float(t["score"]),
                            "context": t["context"],
                            "dyn_conf": bool(t["dyn_confluence"]),
                        }
                        kk = int(sig[i])
                        if kk >= 0:
                            add(
                                "treatment",
                                info,
                                int(t["pos"]) + kk,
                                k,
                                {"thr": thr, "w": w, "detectors": int(cum[i, kk])},
                            )
                        else:
                            add(
                                "unconfirmed",
                                info,
                                int(t["pos"]) + w - 1,
                                k,
                                {"thr": thr, "w": w, "detectors": 0},
                            )
        for k in it.grid_min_stop_k_atr:
            for t in st:
                info = {
                    "touch": int(t["tidx"]), "session": int(t["session"]), "zone_id": t["zone_id"],
                    "direction": int(t["direction"]), "touch_ts": int(t["bar_ts"]),
                    "anchor_type": t["anchor_type"], "score": float(t["score"]),
                    "context": t["context"], "dyn_conf": bool(t["dyn_confluence"]),
                }  # fmt: skip
                add("baseline", info, int(t["pos"]), k, {"thr": 0.0, "w": 0, "detectors": 0})
        rnd = random_setups(sd, rd.bars, setups, score_min, cfg.research.seed + si)
        for k in it.grid_min_stop_k_atr:
            for j, t in enumerate(rnd):
                info = {
                    "touch": -1 - j, "session": t["session"], "zone_id": t["zone_id"],
                    "direction": t["direction"], "touch_ts": t["bar_ts"], "anchor_type": None,
                    "score": None, "context": None, "dyn_conf": False,
                }  # fmt: skip
                add("random", info, t["pos"], k, {"thr": 0.0, "w": 0, "detectors": 0})
        meta["skips"][s] = skips
        meta["random"] = meta.get("random", {}) | {s: len(rnd)}
        del rd
    meta["exclusions"] = excl.summary()
    meta["seconds_total"] = round(time.perf_counter() - t0, 1)
    df = pl.DataFrame(rows, infer_schema_length=None).with_columns(pl.lit("static").alias("pool"))
    return df, meta


def created() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
