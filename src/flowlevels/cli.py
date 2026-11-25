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

    from flowlevels.data.storage import ParquetStore
    from flowlevels.levels.report import pick_days, sanity_plot

    settings, cfg = _setup()
    store = ParquetStore(settings.data_dir)
    for sym in args.symbols:
        for day in pick_days(store, sym, args.n_days, args.seed):
            print(sanity_plot(store, cfg, sym, day, Path(args.out)))
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    rc: int = args.func(args)
    return rc


if __name__ == "__main__":
    sys.exit(main())
