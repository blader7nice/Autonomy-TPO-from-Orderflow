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
        "| symbol | day | status | trades | in-day gaps | missing ids | raw MB | out MB "
        "| download s | process s |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rep["days_report"]:
        missing = sum(g["missing"] for g in r["gaps"])
        lines.append(
            f"| {r['symbol']} | {r['day']} | {r['status']} | {r['n_trades']} | {len(r['gaps'])} "
            f"| {missing} | {r['raw_bytes'] / 1e6:.1f} | {r['out_bytes'] / 1e6:.2f} "
            f"| {r['download_s']} | {r['process_s']} |"
        )
    done = [r for r in rep["days_report"] if r["status"] == "done"]
    lines += [
        "",
        f"Totals over {len(done)} processed days: download "
        f"{sum(r['download_s'] for r in done):.1f} s, processing "
        f"{sum(r['process_s'] for r in done):.1f} s, raw "
        f"{sum(r['raw_bytes'] for r in done) / 1e9:.2f} GB downloaded.",
    ]
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
    boundary = _boundary_clusters(both, tol_v)
    return {
        "minutes_compared": both.height,
        "minutes_only_aggtrades": only_ours,
        "minutes_only_klines": only_klines,
        "minutes_mismatched": mism,
        "minutes_mismatched_boundary": boundary["explained_minutes"],
        "minutes_mismatched_unexplained": boundary["unexplained_minutes"],
        "unexplained_minute_ts": boundary["unexplained_minute_ts"][:20],
        "day_totals": {
            "volume": [float(both["volume"].sum()), tot_v],
            "buy_volume": [float(both["buy_volume"].sum()), float(both["k_buy"].sum())],
            "n_fills": [int(both["n_fills"].sum()), int(both["k_count"].sum())],
        },
        "max_abs_diff": {c[2:]: _max(c) for c in d.columns if c.startswith("d_")},
        "volume_rel_diff_total": (
            abs(float(both["volume"].sum()) - tot_v) / tot_v if tot_v else 0.0
        ),
    }


def _boundary_clusters(both: pl.DataFrame, tol_v: float) -> dict[str, Any]:
    """Classify mismatched minutes (D-023).

    An aggTrade has one timestamp for fills spread over up to 100 ms, while klines bucket
    individual fills, so an aggTrade that straddles a minute boundary moves volume (and
    possibly the extreme/open/close price) into the neighbouring minute. Mismatched minutes
    are grouped into runs of consecutive minutes; a run is explained ("boundary") when its
    signed volume, buy-volume and fill-count differences net to zero. Everything else is
    unexplained and must be investigated.
    """
    if both.height == 0:
        return {"explained_minutes": 0, "unexplained_minutes": 0, "unexplained_minute_ts": []}
    x = both.sort("minute_ts").with_columns(
        (pl.col("volume") - pl.col("k_volume")).alias("sv"),
        (pl.col("buy_volume") - pl.col("k_buy")).alias("sb"),
        (pl.col("n_fills") - pl.col("k_count")).alias("sc"),
    )
    x = x.with_columns(
        (
            (pl.col("open") != pl.col("k_open"))
            | (pl.col("high") != pl.col("k_high"))
            | (pl.col("low") != pl.col("k_low"))
            | (pl.col("close") != pl.col("k_close"))
            | (pl.col("sv").abs() > tol_v)
            | (pl.col("sb").abs() > tol_v)
            | (pl.col("sc") != 0)
        ).alias("bad")
    )
    bad = x.filter(pl.col("bad"))
    if bad.height == 0:
        return {"explained_minutes": 0, "unexplained_minutes": 0, "unexplained_minute_ts": []}
    # A new run starts when the gap to the previous mismatched minute exceeds one minute.
    bad = bad.with_columns(
        (pl.col("minute_ts").diff().fill_null(0) > 60_000).cum_sum().alias("run")
    )
    runs = bad.group_by("run").agg(
        pl.len().alias("n"),
        pl.col("sv").sum().abs().alias("nv"),
        pl.col("sb").sum().abs().alias("nb"),
        pl.col("sc").sum().abs().alias("nc"),
        pl.col("k_volume").sum().alias("kv"),
        pl.col("minute_ts"),
    )
    ok = (pl.col("nv") <= tol_v * pl.col("kv") + 1e-9) & (
        pl.col("nb") <= tol_v * pl.col("kv") + 1e-9
    )
    ok = ok & (pl.col("nc") == 0)
    explained = runs.filter(ok)
    unexplained = runs.filter(~ok)
    return {
        "explained_minutes": int(explained["n"].sum()),
        "unexplained_minutes": int(unexplained["n"].sum()),
        "unexplained_minute_ts": sorted(
            int(t) for ts in unexplained["minute_ts"].to_list() for t in ts
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
        "| symbol | day | compared | only aggTr | only kline | mismatched | boundary "
        "| unexplained | day vol = | day buy = | day fills = | max d_high | max d_low "
        "| max d_vol |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rep["rows"]:
        if "error" in r:
            lines.append(f"| {r['symbol']} | {r['day']} | error: {r['error']} |")
            continue
        m = r["max_abs_diff"]
        t = r["day_totals"]

        def eq(a: list[float]) -> str:
            return "yes" if abs(a[0] - a[1]) <= 1e-9 * max(abs(a[1]), 1.0) else f"{a[0] - a[1]:+g}"

        lines.append(
            f"| {r['symbol']} | {r['day']} | {r['minutes_compared']} | "
            f"{r['minutes_only_aggtrades']} | {r['minutes_only_klines']} | "
            f"{r['minutes_mismatched']} | {r['minutes_mismatched_boundary']} | "
            f"{r['minutes_mismatched_unexplained']} | {eq(t['volume'])} | "
            f"{eq(t['buy_volume'])} | {eq(t['n_fills'])} | {m['high']:g} | {m['low']:g} | "
            f"{m['volume']:g} |"
        )
    ok = [r for r in rep["rows"] if "error" not in r]
    if ok:
        n = sum(r["minutes_compared"] for r in ok)
        mm = sum(r["minutes_mismatched"] for r in ok)
        b = sum(r["minutes_mismatched_boundary"] for r in ok)
        u = sum(r["minutes_mismatched_unexplained"] for r in ok)
        lines += [
            "",
            f"**Total:** {n} minutes compared, {mm} mismatched ({mm / n:.2%}), of which "
            f"{b} are explained by aggTrades straddling a minute boundary (D-023) and "
            f"{u} ({u / n:.4%}) are unexplained.",
        ]
    return "\n".join(lines) + "\n"


def write_report(rep: dict[str, Any], md: str, out_dir: Path, stem: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{stem}.json").write_text(json.dumps(rep, indent=2, default=str))
    p = out_dir / f"{stem}.md"
    p.write_text(md)
    return p
