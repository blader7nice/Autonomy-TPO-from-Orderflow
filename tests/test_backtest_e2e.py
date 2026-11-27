"""End to end on a synthetic store: backtest.run -> report.render (no network, no real data)."""

import datetime as dt
from pathlib import Path

import polars as pl

from flowlevels.config import load_config
from flowlevels.data.storage import ParquetStore
from flowlevels.footprint.minute import footprint_from_trades, ohlcv_from_trades
from flowlevels.research import backtest, report
from tests.helpers import T0, synthetic_trades

ROOT = Path(__file__).resolve().parents[1]
DAY = 86_400_000


def _cfg():  # type: ignore[no-untyped-def]
    cfg = load_config(ROOT / "configs/default.yaml")
    return cfg.model_copy(
        update={
            "signals": cfg.signals.model_copy(update={"resolutions": ["5m", "15m"]}),
            "footprint": cfg.footprint.model_copy(
                update={"baseline_days": 3, "baseline_min_samples": 2}
            ),
            "research": cfg.research.model_copy(
                update={"tuning_days": 3, "bootstrap_reps": 200, "min_n_per_group": 1}
            ),
        }
    )


def test_tuning_run_end_to_end(tmp_path: Path) -> None:
    cfg = _cfg()
    store = ParquetStore(tmp_path)
    tape = synthetic_trades(150_000, seed=3, span_ms=8 * DAY, first_id=1, tick=0.1)
    for sym in ("BTCUSDT",):
        for d in range(8):
            day = dt.date(2026, 1, 1) + dt.timedelta(days=d)
            t = tape.filter(
                (pl.col("ts_ms") >= T0 + d * DAY) & (pl.col("ts_ms") < T0 + (d + 1) * DAY)
            )
            store.write_part("ohlcv_1m", sym, day, "hist", ohlcv_from_trades(t))
            store.write_part("footprint_1m", sym, day, "hist", footprint_from_trades(t, 0.1, 10.0))
            store.mark_complete("ohlcv_1m", sym, day, {"holes": []})
    trades, meta = backtest.run(store, cfg, ["BTCUSDT"])
    assert meta["n_days"] == 3 and meta["sessions"]["BTCUSDT"] == 3
    groups = set(trades["group"].unique().to_list())
    assert {"treatment", "unconfirmed", "baseline", "random"} <= groups
    assert set(trades["pool"].unique().to_list()) == {"static", "dynamic"}
    # Entries are strictly after the touch bar opened (no same-bar entry).
    assert (trades["entry_ts"] > trades["touch_ts"]).all()
    meta |= {
        "run_id": "test",
        "commit": "x",
        "config_hash": "y",
        "created_utc": "z",
        "symbols": ["BTCUSDT"],
        "repairs": [],
    }
    md, summary = report.render(trades, meta, cfg)
    assert summary["n_configs"] == 18
    assert "Configurations tried: 18" in md and "MDE" in md and "hold-out has not been run" in md
