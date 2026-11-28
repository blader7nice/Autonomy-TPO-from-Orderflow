"""Bootstrap (last N days) and kline validation, with markdown/JSON reports."""

from __future__ import annotations

import datetime as dt
import json
import logging
import resource
import shutil
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
    chunk_rows: int | None = None,
    max_disk_pct: float | None = None,
    by_month: bool = False,
) -> dict[str, Any]:
    """Process (symbol, day) pairs. by_month: month by month (all symbols of a month before
    the next month). max_disk_pct: stop before a day if the data disk is fuller than that."""
    t0 = time.perf_counter()
    reports: list[DayReport] = []
    pairs = [(s, d) for s in symbols for d in days]
    if by_month:
        pairs.sort(key=lambda p: (p[1].year, p[1].month, symbols.index(p[0]), p[1]))
    stopped = ""
    for symbol, day in pairs:
        if max_disk_pct is not None:
            store.data_dir.mkdir(parents=True, exist_ok=True)
            du = shutil.disk_usage(store.data_dir)
            pct = 100 * du.used / du.total
            if pct > max_disk_pct:
                stopped = f"disk {pct:.1f} % > {max_disk_pct} % before {symbol} {day}"
                log.error("stopping: %s", stopped)
                break
        try:
            rep = await process_day(
                adapter, store, cfg, symbol, day, raw_dir, keep_raw, force, chunk_rows
            )
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
        "stopped": stopped,
        "peak_rss_gb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6, 2),
        "cross_day_gaps": cross_day_gaps(reports),
    }


def render_bootstrap_md(rep: dict[str, Any]) -> str:
    lines = [
        f"# Bootstrap report ({rep['created_utc']})",
        "",
        f"commit `{rep['commit']}`, config `{rep['config_hash']}`, "
        f"days {' .. '.join(rep['days'])}, total {rep['seconds_total']} s",
        "",
        "| symbol | day | status | trades | in-day gaps | missing ids | time holes (min) "
        "| raw MB | out MB | download s | process s |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rep["days_report"]:
        missing = sum(g["missing"] for g in r["gaps"])
        lines.append(
            f"| {r['symbol']} | {r['day']} | {r['status']} | {r['n_trades']} | {len(r['gaps'])} "
            f"| {missing} | {_holes_cell(r)} | {r['raw_bytes'] / 1e6:.1f} "
            f"| {r['out_bytes'] / 1e6:.2f} "
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
    lines += ["", "## Time holes (no trades for longer than max_trade_silence_s, D-026)", ""]
    hole_lines = [
        f"- {r['symbol']} {r['day']}: "
        + ", ".join(
            f"{dt.datetime.fromtimestamp(h['after_ms'] / 1000, dt.UTC):%H:%M:%S}"
            f"–{dt.datetime.fromtimestamp(h['before_ms'] / 1000, dt.UTC):%H:%M:%S} "
            f"({h['seconds']:.0f} s)"
            for h in r.get("holes", [])
        )
        for r in rep["days_report"]
        if r.get("holes")
    ]
    lines += hole_lines or ["- none"]
    lines += ["", "## Disk", ""]
    lines += [f"- {k}: {v / 1e6:.1f} MB" for k, v in rep["disk_bytes"].items()]
    errors = [r for r in rep["days_report"] if r["status"] in ("error", "missing")]
    if errors:
        lines += ["", "## Errors / missing", ""]
        lines += [f"- {r['symbol']} {r['day']}: {r['status']} {r['error']}" for r in errors]
    return "\n".join(lines) + "\n"


def _holes_cell(r: dict[str, Any]) -> str:
    holes = r.get("holes", [])
    if not holes:
        return "0"
    return f"{len(holes)} ({sum(h['seconds'] for h in holes) / 60:.1f})"


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
    only_klines_ts = [int(t) for t in j.filter(pl.col("open").is_null())["minute_ts"].to_list()]
    only_klines = len(only_klines_ts)
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
        | (pl.col("d_buy") > tol_v)
    ).height
    tot_v = float(both["k_volume"].sum()) if both.height else 0.0
    boundary = _boundary_clusters(both, tol_v, only_klines_ts)
    return {
        "minutes_compared": both.height,
        "minutes_only_aggtrades": only_ours,
        "minutes_only_klines": only_klines,
        "minutes_mismatched": mism,
        "minutes_mismatched_boundary": boundary["explained_minutes"],
        "minutes_mismatched_day_edge": boundary["day_edge_minutes"],
        "minutes_mismatched_hole_edge": boundary["hole_edge_minutes"],
        "minutes_mismatched_unexplained": boundary["unexplained_minutes"],
        "kline_only_minute_ts": only_klines_ts,
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


def _boundary_clusters(
    both: pl.DataFrame, tol_v: float, kline_only_ts: list[int] | None = None
) -> dict[str, Any]:
    """Classify mismatched minutes (D-023, D-026).

    An aggTrade has one timestamp for fills spread over up to 100 ms, while klines bucket
    individual fills, so an aggTrade that straddles a minute boundary moves volume (and
    possibly the extreme/open/close price) into the neighbouring minute. Mismatched minutes
    (OHLC, volume or buy volume) are grouped into runs of consecutive minutes:
    - boundary: the run's signed volume and buy-volume differences net to zero;
    - day_edge: the run touches the first or last minute of the UTC day, so its counterpart
      is in the neighbouring day's file;
    - hole_edge: the run is next to a minute that has kline volume but no aggTrades;
    - unexplained: everything else, to be investigated.
    The fill count (kline `count` vs l - f + 1) is not used: aggTrade fill-id ranges also
    contain ids that klines do not count (DATA_SOURCES.md).
    """
    empty = {
        "explained_minutes": 0,
        "day_edge_minutes": 0,
        "hole_edge_minutes": 0,
        "unexplained_minutes": 0,
        "unexplained_minute_ts": [],
    }
    if both.height == 0:
        return empty
    x = both.sort("minute_ts").with_columns(
        (pl.col("volume") - pl.col("k_volume")).alias("sv"),
        (pl.col("buy_volume") - pl.col("k_buy")).alias("sb"),
    )
    x = x.filter(
        (pl.col("open") != pl.col("k_open"))
        | (pl.col("high") != pl.col("k_high"))
        | (pl.col("low") != pl.col("k_low"))
        | (pl.col("close") != pl.col("k_close"))
        | (pl.col("sv").abs() > tol_v)
        | (pl.col("sb").abs() > tol_v)
    )
    if x.height == 0:
        return empty
    day0 = int(both["minute_ts"].min()) // 86_400_000 * 86_400_000  # type: ignore[arg-type]
    edges = {day0, day0 + 86_400_000 - 60_000}
    holes = set(kline_only_ts or [])
    # A new run starts when more than one matching minute separates two mismatched minutes.
    # Bridging one minute covers chained straddles (m -> m+1 and m+1 -> m+2) where the fill
    # moved into m+1 and the fill moved out of it happen to be equal (verified 2026-11-26
    # against the raw `trades` archive).
    x = x.with_columns((pl.col("minute_ts").diff().fill_null(0) > 120_000).cum_sum().alias("run"))
    runs = x.group_by("run").agg(
        pl.len().alias("n"),
        pl.col("sv").sum().abs().alias("nv"),
        pl.col("sb").sum().abs().alias("nb"),
        pl.col("k_volume").sum().alias("kv"),
        pl.col("minute_ts"),
    )
    out = dict(empty)
    unexplained_ts: list[int] = []
    for r in runs.iter_rows(named=True):
        ts = [int(t) for t in r["minute_ts"]]
        lim = tol_v * r["kv"] + 1e-9
        if r["nv"] <= lim and r["nb"] <= lim:
            out["explained_minutes"] += r["n"]
        elif any(t in edges for t in ts):
            out["day_edge_minutes"] += r["n"]
        elif any(t - 60_000 in holes or t + 60_000 in holes for t in ts):
            out["hole_edge_minutes"] += r["n"]
        else:
            out["unexplained_minutes"] += r["n"]
            unexplained_ts += ts
    out["unexplained_minute_ts"] = sorted(unexplained_ts)
    return out


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
        "| symbol | day | compared | only aggTr | only kline (hole) | mismatched | boundary "
        "| day edge | hole edge | unexplained | day vol = | day buy = | day fills (ours-kline) "
        "| max d_high | max d_low | max d_vol |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
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
            f"{r['minutes_mismatched_day_edge']} | {r['minutes_mismatched_hole_edge']} | "
            f"{r['minutes_mismatched_unexplained']} | {eq(t['volume'])} | "
            f"{eq(t['buy_volume'])} | {t['n_fills'][0] - t['n_fills'][1]:+d} | "
            f"{m['high']:g} | {m['low']:g} | {m['volume']:g} |"
        )
    ok = [r for r in rep["rows"] if "error" not in r]
    if ok:
        n = sum(r["minutes_compared"] for r in ok)
        mm = sum(r["minutes_mismatched"] for r in ok)
        b = sum(r["minutes_mismatched_boundary"] for r in ok)
        de = sum(r["minutes_mismatched_day_edge"] for r in ok)
        he = sum(r["minutes_mismatched_hole_edge"] for r in ok)
        u = sum(r["minutes_mismatched_unexplained"] for r in ok)
        ko = sum(r["minutes_only_klines"] for r in ok)
        lines += [
            "",
            f"**Total:** {n} minutes compared, {mm} mismatched ({mm / n:.2%}): {b} boundary "
            f"straddles netting to zero (D-023), {de} at a UTC day edge, {he} next to a data "
            f"hole, **{u} unexplained ({u / n:.4%})**. {ko} minutes have kline volume but no "
            "aggTrades (data holes, D-026).",
        ]
    return "\n".join(lines) + "\n"


def write_report(rep: dict[str, Any], md: str, out_dir: Path, stem: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{stem}.json").write_text(json.dumps(rep, indent=2, default=str))
    p = out_dir / f"{stem}.md"
    p.write_text(md)
    return p
