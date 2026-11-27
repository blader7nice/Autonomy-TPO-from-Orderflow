"""flowlevels command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys
from collections.abc import Callable, Coroutine
from typing import Any

from flowlevels import __version__
from flowlevels.config import AppConfig, config_hash, git_commit, load_config
from flowlevels.settings import Settings, load_settings


def _setup() -> tuple[Settings, AppConfig]:
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    return settings, load_config(settings.config_path)


def _adapter(settings: Settings) -> Any:
    from flowlevels.venues.binance import BinanceUSDM

    return BinanceUSDM(
        rest_url=settings.binance_rest_url,
        ws_url=settings.binance_ws_url,
        vision_url=settings.binance_vision_url,
        weight_budget_1m=settings.binance_rest_weight_budget_1m,
    )


def _run(coro: Coroutine[Any, Any, int]) -> int:
    return asyncio.run(coro)


def _cmd_info(_: argparse.Namespace) -> int:
    settings, cfg = _setup()
    print(f"flowlevels {__version__}")
    print(f"commit      {git_commit()}")
    print(f"config      {settings.config_path} (v{cfg.version}, hash {config_hash(cfg)})")
    print(f"data_dir    {settings.data_dir}")
    print(f"symbols     {', '.join(cfg.symbols)}")
    return 0


def _cmd_verify_api(args: argparse.Namespace) -> int:
    from flowlevels.venues.verify import verify_api

    settings, _ = _setup()

    async def go() -> int:
        adapter = _adapter(settings)
        try:
            checks = await verify_api(adapter, args.symbols, settings.data_dir / "tmp_verify")
        finally:
            await adapter.aclose()
        out = settings.data_dir / "reports" / f"verify-api-{dt.date.today()}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps([c.__dict__ for c in checks], indent=2, default=str))
        for c in checks:
            print(f"{'PASS' if c.ok else 'FAIL'}  {c.name}")
            print("      " + json.dumps(c.detail, default=str)[:600])
        print(f"\nFull details: {out}")
        return 0 if all(c.ok for c in checks) else 1

    return _run(go())


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    from flowlevels.data.bootstrap import render_bootstrap_md, run_days, write_report
    from flowlevels.data.pipeline import last_complete_days
    from flowlevels.data.storage import ParquetStore

    settings, cfg = _setup()

    async def go() -> int:
        adapter = _adapter(settings)
        try:
            rep = await run_days(
                adapter,
                ParquetStore(settings.data_dir),
                cfg,
                args.symbols,
                last_complete_days(args.days),
                settings.data_dir / "tmp_raw",
                keep_raw=args.keep_raw,
                force=args.force,
            )
        finally:
            await adapter.aclose()
        md = render_bootstrap_md(rep)
        p = write_report(rep, md, settings.data_dir / "reports", f"bootstrap-{dt.date.today()}")
        print(md)
        print(f"Report: {p}")
        bad = [r for r in rep["days_report"] if r["status"] == "error"]
        return 1 if bad else 0

    return _run(go())


def _cmd_validate_klines(args: argparse.Namespace) -> int:
    from flowlevels.data.bootstrap import render_klines_md, validate_klines, write_report
    from flowlevels.data.pipeline import last_complete_days
    from flowlevels.data.storage import ParquetStore

    settings, _ = _setup()

    async def go() -> int:
        adapter = _adapter(settings)
        try:
            rep = await validate_klines(
                adapter,
                ParquetStore(settings.data_dir),
                args.symbols,
                last_complete_days(args.days),
                settings.data_dir / "tmp_raw",
            )
        finally:
            await adapter.aclose()
        md = render_klines_md(rep)
        p = write_report(rep, md, settings.data_dir / "reports", f"klines-{dt.date.today()}")
        print(md)
        print(f"Report: {p}")
        return 0

    return _run(go())


def _cmd_live(args: argparse.Namespace) -> int:
    from flowlevels.data.bootstrap import write_report
    from flowlevels.data.storage import ParquetStore
    from flowlevels.live.collector import run_collector

    settings, cfg = _setup()

    async def go() -> int:
        adapter = _adapter(settings)
        try:
            rep = await run_collector(
                adapter,
                cfg,
                ParquetStore(settings.data_dir),
                args.symbols,
                duration_s=args.duration or None,
                resume=not args.no_resume,
            )
        finally:
            await adapter.aclose()
        md = "# Live smoke\n\n```json\n" + json.dumps(rep, indent=2, default=str) + "\n```\n"
        p = write_report(
            rep, md, settings.data_dir / "reports", f"live-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M}"
        )
        print(md)
        print(f"Report: {p}")
        return 0

    return _run(go())


def _cmd_levels(args: argparse.Namespace) -> int:
    from flowlevels.data.storage import ParquetStore
    from flowlevels.levels.report import levels_at, parse_ts, render_levels

    settings, cfg = _setup()
    print(
        render_levels(
            levels_at(ParquetStore(settings.data_dir), cfg, args.symbol, parse_ts(args.ts))
        )
    )
    return 0


def _cmd_footprint(args: argparse.Namespace) -> int:
    from flowlevels.data.storage import ParquetStore
    from flowlevels.levels.report import parse_ts, render_footprint

    settings, cfg = _setup()
    print(
        render_footprint(
            ParquetStore(settings.data_dir), cfg, args.symbol, parse_ts(args.ts), args.res
        )
    )
    return 0


def _cmd_sanity_plots(args: argparse.Namespace) -> int:
    from pathlib import Path

    from flowlevels.data.quality import Exclusions
    from flowlevels.data.storage import ParquetStore
    from flowlevels.levels.report import pick_days, sanity_plot

    settings, cfg = _setup()
    store = ParquetStore(settings.data_dir)
    excl = Exclusions(holes=[])
    extra = [dt.date.fromisoformat(d) for d in args.day]
    for sym in args.symbols:
        days = pick_days(store, sym, args.n_days, args.seed, cfg.levels, excl)
        for day in sorted(set(days) | set(extra)):
            print(sanity_plot(store, cfg, sym, day, Path(args.out)))
    print(excl.summary())
    return 0


def _cmd_levels_stats(args: argparse.Namespace) -> int:
    from pathlib import Path

    from flowlevels.data.storage import ParquetStore
    from flowlevels.levels.stats import collect, render

    settings, cfg = _setup()
    stats, excl = collect(ParquetStore(settings.data_dir), cfg, args.symbols)
    text = render(stats, excl, cfg, args.title)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
        print(f"Report: {args.out}")
    print(text)
    return 0


def _cmd_backtest(args: argparse.Namespace) -> int:
    from pathlib import Path

    import yaml

    from flowlevels.data.quality import load_repairs
    from flowlevels.data.storage import ParquetStore
    from flowlevels.research import backtest, report

    settings, cfg = _setup()
    store = ParquetStore(settings.data_dir)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-{config_hash(cfg)[:8]}"
    out = settings.data_dir / "research/runs" / run_id
    out.mkdir(parents=True, exist_ok=True)
    trades, meta = backtest.run(store, cfg, args.symbols)
    meta |= {
        "run_id": run_id,
        "kind": "tuning",
        "commit": git_commit(),
        "config_hash": config_hash(cfg),
        "created_utc": report.created(),
        "symbols": args.symbols,
        "repairs": [
            f"{r['symbol']} {r['day']} ({r['check']['fills']} fills, "  # type: ignore[index]
            f"{r['check']['interior_minutes']} minutes kline-exact)"  # type: ignore[index]
            for s in args.symbols
            for r in load_repairs(store, s)
        ],
    }
    md, summary = report.render(trades, meta, cfg)
    trades.write_parquet(out / "trades.parquet", compression="zstd")
    (out / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False))
    (out / "meta.json").write_text(json.dumps(meta | {"summary": summary}, indent=2, default=str))
    (out / "REPORT.md").write_text(md)
    if args.copy_to:
        Path(args.copy_to).mkdir(parents=True, exist_ok=True)
        (Path(args.copy_to) / f"REPORT-{run_id}.md").write_text(md)
    print(md)
    print(f"Run dir: {out}")
    return 0


def _cmd_iteration1(args: argparse.Namespace) -> int:
    from pathlib import Path

    import yaml

    from flowlevels.data.quality import Exclusions
    from flowlevels.data.storage import ParquetStore
    from flowlevels.research import iteration1, iteration1_report
    from flowlevels.research.backtest import load_symbol, tuning_days

    settings, cfg = _setup()
    store = ParquetStore(settings.data_dir)
    if args.median_only:
        # Zones only (no outcomes): the frozen setup-filter threshold (ITERATION-1).
        days = tuning_days(store, args.symbols, cfg.research.tuning_days)
        ex = Exclusions(holes=[])
        ss = [load_symbol(store, cfg, s, days, ex).sessions for s in args.symbols]
        n = sum(len(sz.zones) for x in ss for sz in x.values())
        print(f"static zones: {n}, median score: {iteration1.zone_score_median(ss):g}")
        return 0
    if cfg.iteration1.min_zone_score is None:
        print("iteration1.min_zone_score must be frozen in the config first (--median-only)")
        return 2
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-it1-{config_hash(cfg)[:8]}"
    out = settings.data_dir / "research/runs" / run_id
    out.mkdir(parents=True, exist_ok=True)
    trades, meta = iteration1.run(store, cfg, args.symbols)
    meta |= {
        "run_id": run_id,
        "kind": "iteration1",
        "commit": git_commit(),
        "config_hash": config_hash(cfg),
        "created_utc": iteration1.created(),
        "symbols": args.symbols,
    }
    md, summary = iteration1_report.render(trades, meta, cfg)
    trades.write_parquet(out / "trades.parquet", compression="zstd")
    (out / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False))
    (out / "meta.json").write_text(json.dumps(meta | {"summary": summary}, indent=2, default=str))
    (out / "REPORT.md").write_text(md)
    if args.copy_to:
        Path(args.copy_to).mkdir(parents=True, exist_ok=True)
        (Path(args.copy_to) / f"REPORT-{run_id}.md").write_text(md)
    print(md)
    print(f"Run dir: {out}")
    return 0


Handler = Callable[[argparse.Namespace], int]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flowlevels")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn: Handler, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.set_defaults(func=fn)
        return p

    add("info", _cmd_info, "Print version, commit and config hash")
    p = add("verify-api", _cmd_verify_api, "Check Binance endpoint assumptions (network)")
    p.add_argument("--symbols", nargs="+", default=["BTCUSDT"])
    p = add("bootstrap", _cmd_bootstrap, "Download + process the last N days (network)")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    p.add_argument("--keep-raw", action="store_true")
    p.add_argument("--force", action="store_true", help="Reprocess completed days")
    p = add("validate-klines", _cmd_validate_klines, "Compare rebuilt 1m OHLCV with klines")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    p = add("live", _cmd_live, "Run the live collector (network)")
    p.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    p.add_argument("--duration", type=float, default=0, help="Seconds; 0 = forever")
    p.add_argument("--no-resume", action="store_true")
    p = add("levels", _cmd_levels, "Print levels and zones as of a timestamp (UTC)")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--ts", required=True, help="ISO time, e.g. 2026-11-20T14:00")
    p = add("footprint", _cmd_footprint, "Print the footprint of the bar containing --ts")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--ts", required=True)
    p.add_argument("--res", default="5m", choices=["1m", "3m", "5m", "15m", "1h"])
    p = add("sanity-plots", _cmd_sanity_plots, "PNG of random days with levels + VWAP bands")
    p.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    p.add_argument("--n-days", type=int, default=3)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default="docs/sanity")
    p.add_argument("--day", action="append", default=[], help="Also plot this day (repeat)")
    p = add("levels-stats", _cmd_levels_stats, "Levels/zones per day and zone widths (report)")
    p.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    p.add_argument("--out", default="")
    p.add_argument("--title", default="Level statistics")
    p = add("backtest", _cmd_backtest, "Phase 3 tuning-window run (in-sample report)")
    p.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    p.add_argument("--copy-to", default="docs/research", help="Also copy REPORT.md here")
    p = add("iteration1", _cmd_iteration1, "ITERATION-1 in-sample run (pre-registered)")
    p.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    p.add_argument("--copy-to", default="docs/research")
    p.add_argument("--median-only", action="store_true", help="Print the zone-score median")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    rc: int = args.func(args)
    return rc


if __name__ == "__main__":
    sys.exit(main())
