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


def holdout_days_in(cfg: AppConfig, days: list[dt.date]) -> list[dt.date]:
    """Days of `days` inside the hold-out window (must be empty for any aggTrade run)."""
    return [d for d in days if cfg.plan.holdout_start <= d <= cfg.plan.holdout_end]


def _cmd_bootstrap_range(args: argparse.Namespace) -> int:
    """ITERATION-4: aggTrades -> 1m footprint for an explicit date range, month by month,
    streamed in batches (bounded memory), raw deleted per day. Refuses hold-out days."""
    from flowlevels.data.bootstrap import render_bootstrap_md, run_days, write_report
    from flowlevels.data.storage import ParquetStore

    settings, cfg = _setup()
    start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    bad = holdout_days_in(cfg, days)
    if bad:
        print(f"REFUSED: {len(bad)} day(s) inside the hold-out ({bad[0]} .. {bad[-1]})")
        return 2
    i4 = cfg.iteration4

    async def go() -> int:
        adapter = _adapter(settings)
        try:
            rep = await run_days(
                adapter,
                ParquetStore(settings.data_dir),
                cfg,
                args.symbols,
                days,
                settings.data_dir / "tmp_raw",
                chunk_rows=args.chunk_rows or i4.chunk_rows,
                max_disk_pct=i4.max_disk_pct,
                by_month=True,
            )
        finally:
            await adapter.aclose()
        md = render_bootstrap_md(rep)
        md += f"\nPeak RSS {rep['peak_rss_gb']} GB; stopped: {rep['stopped'] or 'no'}.\n"
        p = write_report(rep, md, settings.data_dir / "reports", f"bootstrap-range-{start}-{end}")
        print(md[-3000:])
        print(f"Report: {p}")
        errs = [r for r in rep["days_report"] if r["status"] in ("error", "missing")]
        return 1 if errs or rep["stopped"] else 0

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


def _cmd_klines_fill(args: argparse.Namespace) -> int:
    from flowlevels.data import klines

    settings, _ = _setup()
    start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)

    async def go() -> int:
        adapter = _adapter(settings)
        try:
            for s in args.symbols:
                root = settings.data_dir / "klines"
                paths = await klines.fill_range(adapter, root, s, start, end)
                print(f"{s}: {len(paths)} month files")
        finally:
            await adapter.aclose()
        return 0

    return _run(go())


def _cmd_sz_fidelity(args: argparse.Namespace) -> int:
    """Replay every logged Supply Zone scan with the port (candles only)."""
    from pathlib import Path

    import numpy as np
    import polars as pl

    from flowlevels.strategies import sz_fidelity as fid

    settings, cfg = _setup()
    out = settings.data_dir / "research/fidelity"
    out.mkdir(parents=True, exist_ok=True)
    rows = fid.read_log(Path(args.log))
    df = fid.replay(rows, settings.data_dir / "klines", cfg.supply_zone)
    df.write_parquet(out / "replay.parquet")
    m = fid.metrics(df)
    (out / "metrics.json").write_text(json.dumps(m, indent=2, default=str))
    print(json.dumps(m, indent=2, default=str))
    if args.dump_sample:
        # reference sample: every scan where log or port reached the core signal + random others
        d = df.filter(pl.col("p_reason") != "no_klines")
        interesting = d.filter(
            (~pl.col("p_reason").is_in(list(fid.NON_CORE_PORT)))
            | (~pl.col("reason").is_in(["no_strategy_signal", "consolidation_regime"]))
        )
        rest = d.join(interesting.select("symbol", "ts"), on=["symbol", "ts"], how="anti")
        rng = np.random.default_rng(cfg.research.seed)
        pick = rng.choice(rest.height, size=min(args.dump_sample, rest.height), replace=False)
        sample = pl.concat([interesting, rest[np.sort(pick)]]).select("symbol", "ts").unique()
        fid.dump_frames(
            list(sample.iter_rows()), settings.data_dir / "klines", cfg.supply_zone,
            out / "ref_frames.npz",
        )  # fmt: skip
        print(f"reference frames: {sample.height} scans -> {out / 'ref_frames.npz'}")
    return 0


def _cmd_sz_refcheck(args: argparse.Namespace) -> int:
    from pathlib import Path

    from flowlevels.strategies import sz_fidelity as fid

    _, cfg = _setup()
    r = fid.refcheck(Path(args.frames), Path(args.ref), cfg.supply_zone)
    Path(args.out).write_text(json.dumps(r, indent=2, default=str))
    print(json.dumps({k: v for k, v in r.items() if k != "diffs"}, indent=2))
    for x in r["diffs"][:15]:
        print(x)
    return 0


def _cmd_plan_run(args: argparse.Namespace) -> int:
    """MASTER-PLAN phase A (Supply Zone) or B (Dalton) on windows A and B (candles only)."""
    from pathlib import Path

    import yaml

    from flowlevels.research import plan

    settings, cfg = _setup()
    root = settings.data_dir / "klines"
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-plan{args.phase}-{config_hash(cfg)[:8]}"
    out = settings.data_dir / "research/runs" / run_id
    out.mkdir(parents=True, exist_ok=True)
    if args.phase == "a":
        tr, meta = plan.sz_trades(cfg, root)
        md, summary = plan.phase_a_report(tr, meta, cfg)
    else:
        tr, meta = plan.dalton_trades(cfg, root)
        md, summary = plan.phase_b_report(tr, meta, cfg)
    head = (
        f"# MASTER-PLAN phase {args.phase.upper()} — run `{run_id}`\n\nCommit `{git_commit()}`, "
        f"config `{config_hash(cfg)}`. Windows A {cfg.plan.a_start} … {cfg.plan.a_end}, B "
        f"{cfg.plan.b_start} … {cfg.plan.b_end}; hold-out {cfg.plan.holdout_start} … "
        f"{cfg.plan.holdout_end} not used. Costs `{cfg.costs.fee_profile}` + "
        f"`{cfg.costs.slippage}`."
        "\n\n"
    )
    md = head + md
    if tr.height:
        tr.write_parquet(out / "trades.parquet", compression="zstd")
    (out / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False))
    (out / "meta.json").write_text(
        json.dumps(meta | {"summary": summary, "commit": git_commit()}, indent=2, default=str)
    )
    (out / "REPORT.md").write_text(md)
    Path(args.copy_to).mkdir(parents=True, exist_ok=True)
    (Path(args.copy_to) / f"PLAN-{args.phase.upper()}-{run_id}.md").write_text(md)
    print(md)
    print(f"Run dir: {out}")
    return 0


def _cmd_iter4_run(args: argparse.Namespace) -> int:
    """ITERATION-4 (FINAL) on windows A and B: grid, kill rule, report, example charts."""
    import resource
    import time
    from pathlib import Path

    import yaml

    from flowlevels.data.storage import ParquetStore
    from flowlevels.research import iter4

    settings, cfg = _setup()
    t0 = time.perf_counter()
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-iter4-{config_hash(cfg)[:8]}"
    out = settings.data_dir / "research/runs" / run_id
    out.mkdir(parents=True, exist_ok=True)
    kroot = settings.data_dir / "klines"
    tr, meta = iter4.iter4_trades(cfg, kroot, ParquetStore(settings.data_dir))
    md, summary, sel = iter4.report(tr, meta, cfg)
    secs = time.perf_counter() - t0
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
    head = (
        f"# ITERATION-4 (FINAL) — run `{run_id}`\n\nCommit `{git_commit()}`, config "
        f"`{config_hash(cfg)}`. Pre-registration: `docs/research/ITERATION-4-PREREG.md`. "
        f"Windows A {cfg.plan.a_start} … {cfg.plan.a_end}, B {cfg.plan.b_start} … "
        f"{cfg.plan.b_end}; hold-out {cfg.plan.holdout_start} … {cfg.plan.holdout_end} not "
        f"used (0 runs). Costs `{cfg.costs.fee_profile}` + `{cfg.costs.slippage}`. "
        f"Run time {secs:.0f} s, peak RSS {rss:.2f} GB.\n\n"
    )
    md = head + md
    if tr.height:
        tr.write_parquet(out / "trades.parquet", compression="zstd")
    (out / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False))
    meta_out = {k: {kk: vv for kk, vv in v.items() if kk != "c2_keys"} for k, v in meta.items()}
    (out / "meta.json").write_text(
        json.dumps(meta_out | {"summary": summary, "commit": git_commit()}, indent=2, default=str)
    )
    (out / "REPORT.md").write_text(md)
    dest = Path(args.copy_to)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"ITER4-{run_id}.md").write_text(md)
    if sel is not None and tr.height:
        ex = iter4.example_trades(tr, sel, cfg, kroot, dest / "iter4")
        sel_note = (
            f"selected configuration `{sel}`"
            if summary.get("selected")
            else f"configuration `{sel}` (most trades; none reached the minimum)"
        )
        (dest / "TRADES-ITER4.md").write_text(
            f"# ITERATION-4 example trades — run `{run_id}`\n\n"
            f"10 trades of the {sel_note}: the 3 best, the 3 worst and 4 around the median net R "
            "(default costs). Verdict: "
            f"**{summary['verdict']}** ({summary['reason']}).\n\n"
            "Legend: grey candles = 15m (hollow = up); yellow bands = the sweep bar and the "
            "close-back (confirmation) bar; orange dashed line / band = the POI(s) (level / zone); "
            "violet dotted = the 15m swing that had to be swept; blue / red / green = entry / stop / "
            "target from the entry bar on; X = exit. Bias: b1 shows the 4H EMA50 as known at each "
            "4H close; b2 marks the 4H fractal swings (▼ high, ▲ low) at their confirmation time "
            "(2 bars after the swing bar).\n\n" + ex
        )
    print(md)
    print(f"Run dir: {out}")
    return 0


def _cmd_paper_ref_run(args: argparse.Namespace) -> int:
    """Paper-test reference backtest: the four paper variants on A and B (variant c is new).
    Never touches the hold-out (window loader guards)."""
    import resource
    import time
    from pathlib import Path

    import yaml

    from flowlevels.research import iter5, paper_ref

    settings, cfg = _setup()
    t0 = time.perf_counter()
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-paperref-{config_hash(cfg)[:8]}"
    out = settings.data_dir / "research/runs" / run_id
    out.mkdir(parents=True, exist_ok=True)
    kroot = settings.data_dir / "klines"
    tr, meta = paper_ref.ref_trades(cfg, kroot)
    check, ok = paper_ref.consistency(tr, Path(args.iter5_trades) if args.iter5_trades else None)
    md, summary = paper_ref.report(tr, meta, cfg, check)
    secs = time.perf_counter() - t0
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
    head = (
        f"# Paper-test reference backtest — run `{run_id}`\n\nCommit `{git_commit()}`, config "
        f"`{config_hash(cfg)}`. Plan: DECISIONS `PAPER-PLAN`. Windows A {cfg.plan.a_start} … "
        f"{cfg.plan.a_end}, B {cfg.plan.b_start} … {cfg.plan.b_end}; hold-out "
        f"{cfg.plan.holdout_start} … {cfg.plan.holdout_end} not scored (0 runs; B's warm-up uses "
        f"hold-out-span candles as indicator input only). Costs `{cfg.costs.fee_profile}` + "
        f"`{cfg.costs.slippage}`. Run time {secs:.0f} s, peak RSS {rss:.2f} GB. This is a "
        "reference run for the paper test; it changes no verdict (FINAL-KILL stands).\n\n"
    )
    md = head + md
    tr.write_parquet(out / "trades.parquet", compression="zstd")
    (out / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False))
    (out / "meta.json").write_text(
        json.dumps(
            {"skips": meta, "summary": summary, "consistent": ok, "commit": git_commit()},
            indent=2,
            default=str,
        )
    )
    (out / "REPORT.md").write_text(md)
    dest = Path(args.copy_to)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"PAPER-REF-{run_id}.md").write_text(md)
    ex = iter5.example_trades(tr, "T2|wick|trail", cfg, kroot, dest / "paperref")
    (dest / "TRADES-PAPER-REF.md").write_text(
        f"# Paper variant c `T2|wick|trail` example trades — run `{run_id}`\n\n"
        "10 trades (3 best, 3 worst, 4 around the median net R, default costs) to check the wick "
        "stop visually. Same chart legend as `TRADES-ITER5.md`.\n\n" + ex
    )
    print(md)
    print(f"Run dir: {out}; consistent: {ok}")
    return 0 if ok is not False else 1


def _cmd_iter5_run(args: argparse.Namespace) -> int:
    """ITERATION-5 (FINAL) on windows A and B: grid, gate, report, example charts.
    Never touches the hold-out (window loader guards); the hold-out run is a separate step."""
    import resource
    import time
    from pathlib import Path

    import yaml

    from flowlevels.research import iter5

    settings, cfg = _setup()
    t0 = time.perf_counter()
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-iter5-{config_hash(cfg)[:8]}"
    out = settings.data_dir / "research/runs" / run_id
    out.mkdir(parents=True, exist_ok=True)
    kroot = settings.data_dir / "klines"
    tr, meta = iter5.iter5_trades(cfg, kroot)
    md, summary, sel = iter5.report(tr, meta, cfg)
    secs = time.perf_counter() - t0
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
    head = (
        f"# ITERATION-5 (FINAL, binding hold-out) — run `{run_id}`\n\nCommit `{git_commit()}`, "
        f"config `{config_hash(cfg)}`. Pre-registration: `docs/research/ITERATION-5-PREREG.md`. "
        f"Windows A {cfg.plan.a_start} … {cfg.plan.a_end}, B {cfg.plan.b_start} … "
        f"{cfg.plan.b_end}; hold-out {cfg.plan.holdout_start} … {cfg.plan.holdout_end} not "
        f"scored (0 runs; B's warm-up uses hold-out-span candles as indicator input only). "
        f"Symbols {', '.join(cfg.iteration5.symbols)}. Costs `{cfg.costs.fee_profile}` + "
        f"`{cfg.costs.slippage}`. Run time {secs:.0f} s, peak RSS {rss:.2f} GB.\n\n"
    )
    md = head + md
    if tr.height:
        tr.write_parquet(out / "trades.parquet", compression="zstd")
    (out / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False))
    (out / "meta.json").write_text(
        json.dumps(meta | {"summary": summary, "commit": git_commit()}, indent=2, default=str)
    )
    (out / "REPORT.md").write_text(md)
    dest = Path(args.copy_to)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"ITER5-{run_id}.md").write_text(md)
    if sel is not None and tr.height:
        ex = iter5.example_trades(tr, sel, cfg, kroot, dest / "iter5")
        sel_note = (
            f"selected configuration `{sel}`"
            if summary.get("selected")
            else f"configuration `{sel}` (most trades; none reached the minimum)"
        )
        (dest / "TRADES-ITER5.md").write_text(
            f"# ITERATION-5 example trades — run `{run_id}`\n\n"
            f"10 trades of the {sel_note}: the 3 best, the 3 worst and 4 around the median net R "
            f"(default costs). Verdict: **{summary['verdict']}** ({summary['reason']}).\n\n"
            "Legend: grey candles = 15m (T1) or 30m (T2), hollow = up; yellow bands = the sweep "
            "and close-back bars (T1) or the touch … confirmation bars (T2); orange dashed line / "
            "band = the POI(s) (level / zone), the caption names them and the far edge that "
            "anchors the stop; violet dotted = the 15m swing that had to be swept (T1); blue / "
            "red / green = entry / stop / TP1 (dashed green = TP2 for `lvl`) from the entry on; "
            "X = the two half exits. Bias: teal = 4H EMA50 and magenta = daily EMA50, each as "
            "known at its bar close; the subtitle gives the values at the entry.\n\n" + ex
        )
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
    p = add("bootstrap-range", _cmd_bootstrap_range, "aggTrades -> footprint for a date range")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    p.add_argument("--chunk-rows", type=int, default=0)
    p = add("validate-klines", _cmd_validate_klines, "Compare rebuilt 1m OHLCV with klines")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    p = add("klines-fill", _cmd_klines_fill, "Store 1m klines (candles only) for a date range")
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p = add("sz-fidelity", _cmd_sz_fidelity, "Replay the Supply Zone live log (candles only)")
    p.add_argument("--log", required=True)
    p.add_argument("--dump-sample", type=int, default=0)
    p = add("sz-refcheck", _cmd_sz_refcheck, "Port vs original pandas code on identical frames")
    p.add_argument("--frames", required=True)
    p.add_argument("--ref", required=True)
    p.add_argument("--out", required=True)
    p = add("iter4-run", _cmd_iter4_run, "ITERATION-4 (FINAL) grid, report and example charts")
    p.add_argument("--copy-to", default="docs/research")
    p = add("paper-ref-run", _cmd_paper_ref_run, "Paper variants reference backtest on A and B")
    p.add_argument("--copy-to", default="docs/research")
    p.add_argument(
        "--iter5-trades",
        default="data/research/runs/20261127T194620Z-iter5-da431f34/trades.parquet",
    )
    p = add("iter5-run", _cmd_iter5_run, "ITERATION-5 (FINAL) in-sample grid, gate, report, charts")
    p.add_argument("--copy-to", default="docs/research")
    p = add("plan-run", _cmd_plan_run, "MASTER-PLAN phase a (Supply Zone) or b (Dalton)")
    p.add_argument("--phase", choices=["a", "b"], required=True)
    p.add_argument("--copy-to", default="docs/research")
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
