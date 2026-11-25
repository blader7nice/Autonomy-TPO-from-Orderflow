"""Bootstrap (last N days) and kline validation, with markdown/JSON reports."""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Any

import polars as pl

from flowlevels.config import AppConfig, config_hash, git_commit
from flowlevels.data.pipeline import DayReport, cross_day_gaps, process_day
from flowlevels.data.storage import ParquetStore, dir_size_bytes
from flowlevels.venues.base import DataNotAvailableError, VenueAdapter

log = logging.getLogger(__name__)


async def run_days(
    adapter: VenueAdapter,
    store: ParquetStore,
    cfg: AppConfig,
    symbols: list[str],
    days: list[dt.date],
    raw_dir: Path,
    keep_raw: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    reports: list[DayReport] = []
    for symbol in symbols:
        for day in days:
            try:
                rep = await process_day(adapter, store, cfg, symbol, day, raw_dir, keep_raw, force)
            except Exception as exc:  # report and continue with the next day
                log.exception("failed %s %s", symbol, day)
                rep = DayReport(symbol, day.isoformat(), "error", error=repr(exc))
            log.info(
                "%s %s %s trades=%d gaps=%d %.1fs",
                symbol,
                day,
                rep.status,
                rep.n_trades,
                len(rep.gaps),
                rep.seconds,
            )
            reports.append(rep)
    return {
        "kind": "bootstrap",
        "created_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "commit": git_commit(),
        "config_hash": config_hash(cfg),
        "symbols": symbols,
        "days": [days[0].isoformat(), days[-1].isoformat()] if days else [],
        "seconds_total": round(time.perf_counter() - t0, 1),
        "disk_bytes": {
            "footprint_1m": dir_size_bytes(store.data_dir / "derived/footprint_1m"),
            "ohlcv_1m": dir_size_bytes(store.data_dir / "derived/ohlcv_1m"),
            "raw": dir_size_bytes(raw_dir),
        },
        "days_report": [r.to_dict() for r in reports],
        "cross_day_gaps": cross_day_gaps(reports),
    }


def render_bootstrap_md(rep: dict[str, Any]) -> str:
    lines = [
        f"# Bootstrap report ({rep['created_utc']})",
        "",
        f"commit `{rep['commit']}`, config `{rep['config_hash']}`, "
        f"days {' .. '.join(rep['days'])}, total {rep['seconds_total']} s",
        "",
        "| symbol | day | status | trades | in-day gaps | missing ids | raw MB | out MB | s |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rep["days_report"]:
        missing = sum(g["missing"] for g in r["gaps"])
        lines.append(
            f"| {r['symbol']} | {r['day']} | {r['status']} | {r['n_trades']} | {len(r['gaps'])} "
            f"| {missing} | {r['raw_bytes'] / 1e6:.1f} | {r['out_bytes'] / 1e6:.2f} "
            f"| {r['seconds']} |"
        )
    lines += ["", "## Cross-day id gaps", ""]
    lines += [f"- {g}" for g in rep["cross_day_gaps"]] or ["- none"]
    lines += ["", "## Disk", ""]
    lines += [f"- {k}: {v / 1e6:.1f} MB" for k, v in rep["disk_bytes"].items()]
    errors = [r for r in rep["days_report"] if r["status"] in ("error", "missing")]
    if errors:
        lines += ["", "## Errors / missing", ""]
        lines += [f"- {r['symbol']} {r['day']}: {r['status']} {r['error']}" for r in errors]
    return "\n".join(lines) + "\n"


def compare_klines(ohlcv: pl.DataFrame, klines: pl.DataFrame) -> dict[str, Any]:
    """Compare 1m OHLCV rebuilt from aggTrades with exchange 1m klines."""
    k = klines.select(
        pl.col("open_time").alias("minute_ts"),
        pl.col("open").alias("k_open"),
        pl.col("high").alias("k_high"),
        pl.col("low").alias("k_low"),
        pl.col("close").alias("k_close"),
        pl.col("volume").alias("k_volume"),
        pl.col("taker_buy_volume").alias("k_buy"),
        pl.col("count").alias("k_count"),
    ).filter(pl.col("k_volume") > 0)  # klines with no trades have no aggTrades either
    j = ohlcv.join(k, on="minute_ts", how="full", coalesce=True)
    only_ours = j.filter(pl.col("k_open").is_null()).height
    only_klines = j.filter(pl.col("open").is_null()).height
    both = j.drop_nulls(["open", "k_open"])
    d = both.select(
        (pl.col("open") - pl.col("k_open")).abs().alias("d_open"),
        (pl.col("high") - pl.col("k_high")).abs().alias("d_high"),
        (pl.col("low") - pl.col("k_low")).abs().alias("d_low"),
        (pl.col("close") - pl.col("k_close")).abs().alias("d_close"),
        (pl.col("volume") - pl.col("k_volume")).abs().alias("d_volume"),
        (pl.col("buy_volume") - pl.col("k_buy")).abs().alias("d_buy"),
        (pl.col("n_fills") - pl.col("k_count")).abs().alias("d_count"),
        pl.col("k_volume"),
    )
    tol_v = 1e-6

    def _max(c: str) -> float:
        v = d[c].max() if d.height else 0.0
        return float(v) if v is not None else 0.0  # type: ignore[arg-type]

    mism = d.filter(
        (pl.col("d_open") > 0)
        | (pl.col("d_high") > 0)
        | (pl.col("d_low") > 0)
        | (pl.col("d_close") > 0)
        | (pl.col("d_volume") > tol_v)
    ).height
    tot_v = float(both["k_volume"].sum()) if both.height else 0.0
    return {
        "minutes_compared": both.height,
        "minutes_only_aggtrades": only_ours,
        "minutes_only_klines": only_klines,
        "minutes_mismatched": mism,
        "max_abs_diff": {c[2:]: _max(c) for c in d.columns if c.startswith("d_")},
        "volume_rel_diff_total": (
            abs(float(both["volume"].sum()) - tot_v) / tot_v if tot_v else 0.0
        ),
    }


async def validate_klines(
    adapter: VenueAdapter,
    store: ParquetStore,
    symbols: list[str],
    days: list[dt.date],
    raw_dir: Path,
) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    for symbol in symbols:
        for day in days:
            row: dict[str, Any] = {"symbol": symbol, "day": day.isoformat()}
            try:
                ohlcv = store.read("ohlcv_1m", symbol, day, day)
                path = await adapter.download_klines_1m_day(symbol, day, raw_dir)
                klines = adapter.read_klines_file(path)
                path.unlink(missing_ok=True)
                row.update(compare_klines(ohlcv, klines))
            except (FileNotFoundError, DataNotAvailableError) as exc:
                row["error"] = repr(exc)
            out.append(row)
    return {"kind": "kline_validation", "rows": out}


def render_klines_md(rep: dict[str, Any]) -> str:
    lines = [
        "# 1m kline validation",
        "",
        "| symbol | day | compared | only aggTr | only kline | mismatched | max d_high | "
        "max d_low | max d_vol | vol rel diff | max d_count |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rep["rows"]:
        if "error" in r:
            lines.append(f"| {r['symbol']} | {r['day']} | error: {r['error']} |")
            continue
        m = r["max_abs_diff"]
        lines.append(
            f"| {r['symbol']} | {r['day']} | {r['minutes_compared']} | "
            f"{r['minutes_only_aggtrades']} | {r['minutes_only_klines']} | "
            f"{r['minutes_mismatched']} | {m['high']:g} | {m['low']:g} | {m['volume']:g} | "
            f"{r['volume_rel_diff_total']:.2e} | {m['count']:g} |"
        )
    return "\n".join(lines) + "\n"


def write_report(rep: dict[str, Any], md: str, out_dir: Path, stem: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{stem}.json").write_text(json.dumps(rep, indent=2, default=str))
    p = out_dir / f"{stem}.md"
    p.write_text(md)
    return p
