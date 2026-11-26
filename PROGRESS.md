# PROGRESS

Current phase: **3 — detectors + 60-day tuning backtest** (next; runs on the VPS after Phase 1 acceptance)

## Phase 0 — Repo skeleton ✅ (2026-11-25)
- Done: uv project (Python 3.12, `uv.lock`), package layout `src/flowlevels/*`, typed settings
  (`flowlevels.settings`, fail-fast), versioned YAML config with a hash
  (`flowlevels.config`), CLI, Makefile, CI (ruff, mypy, pytest), AGENTS.md, docs/SPEC.md
  (amended spec), DECISIONS, BACKLOG, DATA_SOURCES.
- Open: the Binance WS/REST docs could not be reached from the dev container, so every
  such fact is UNVERIFIED (D-009).

## Phase 1 — Venue adapter + bootstrap + live collector ✅ code / ⏳ VPS acceptance (2026-11-25)
- Done:
  - `VenueAdapter` protocol and `BinanceUSDM`: data.binance.vision download with sha256
    CHECKSUM verification, HTTP Range resume and retry/backoff; aggTrades and klines CSV
    parsers (header detected automatically, ms timestamps asserted); REST `aggTrades`
    fromId; WS combined-stream parser (aggTrade, forceOrder).
  - `TradeSource` with `ReplaySource` and `LiveSource`. LiveSource handles reconnects
    (exponential backoff with jitter), proactive reconnect at the lifetime limit (23 h),
    the BTC 60 s watchdog, duplicate dropping, and id-gap REST backfill with resolved and
    unresolved gap logging.
  - `MinuteBuilder` (1m footprint and OHLCV, batch-invariant, exact Σpv/Σp²v),
    `Ingestor` (the shared history/live code path), `MemorySink`, and
    `HourlyParquetSink` for live.
  - Streaming day pipeline (download → verify → build → write → `_SUCCESS` marker →
    delete raw). It is idempotent and rebuilds on a config fingerprint change. It
    reports in-day and cross-day id gaps.
  - CLI and make targets: `verify-api`, `bootstrap`, `validate-klines`, `live`
    (`live-smoke`).
- Verified (on synthetic fixtures only, 41 tests): aggressor mapping in CSV, REST and WS;
  hand-computed footprint and OHLCV; batch-size invariance (hypothesis); replay vs live
  give identical minutes; checksum mismatch is rejected; download resume; skip on re-run;
  gap detection in-day and cross-day; live gap backfill after a reconnect; backfill
  failure logged as unresolved; resume from a stored id; watchdog; lifetime reconnect;
  collector restart safety; kline comparison.
- Numbers: synthetic 3M-trade day takes 0.46 s to parse and 0.29 s to aggregate (dev
  container); footprint is about 15k rows and 0.2 MB zstd per day (synthetic, sparser
  than real data).
- **VPS acceptance, step 1: `make verify-api` ✅ (2026-11-26).** 11/11 checks PASS. Every
  item in DATA_SOURCES.md is now VERIFIED, with sources and live evidence. Reality differed
  in 5 places, all fixed with regression tests (`tests/test_binance_verified.py`, 15 tests):
  1. WS: the unrouted `wss://fstream.binance.com/stream` is **silent** for aggTrade and
     forceOrder since 2026-04-23. Default is now `.../market`, and settings reject unrouted
     URLs (D-021). This was the cause of the first verify-api FAIL.
  2. aggTrade has new fields `nq` (excludes RPI) and `st`. We keep `q`, which is RPI-inclusive
     and equals the archive id by id (D-020).
  3. forceOrder sends the *largest* liquidation per symbol per 1 s, not the latest (SPEC §4.4).
  4. REST aggTrades serves only the last 48 h (HTTP 400 −4166). The backfill now logs
     `beyond_rest_lookback` (D-022).
  5. REST weight is 20 per request against an IP limit of 2400/min. The old 0.05 s pacing
     would have used ~24k/min on the IP shared with the trading bots. Now 1 req/s, plus a
     used-weight budget guard (1200/min by default) (D-022).
  - `m` semantics confirmed in the docs and empirically: buyer-aggressor volume equals the
    kline taker-buy volume exactly over a full day (117946.8 BTC). The flipped mapping matches
    0% of minutes. Per-minute residual: aggTrades that straddle a minute boundary (D-023).
- **Pending on the VPS (acceptance):**
  `make verify-api` → `make bootstrap` → `make validate-klines` → `make live-smoke`.
  Record the results here: per-day timing, disk use, gap report, kline residuals, the
  1h live gap/reconnect log. Then update DATA_SOURCES.md from UNVERIFIED to VERIFIED.
- Open issues / risks:
  - Every WS/REST payload assumption is UNVERIFIED (D-009).
  - Possible kline residuals to investigate on real data: trades in klines that are not
    in aggTrades (for example RPI or special matches), if any. Not verified.

## Phase 2 — Footprint + levels ✅ code / ⏳ real-data sanity plots (2026-11-25)
- Done:
  - `footprint.bars`: exact 1m → 3m/5m/15m/1h aggregation of footprint and OHLCV;
    delta, CVD (continuous and session-reset); contiguous ladders; diagonal and stacked
    imbalances; bar POC.
  - `levels.profile`: TPO and volume profiles, POC and value area (dual-row rule, ties),
    HVN/LVN.
  - `levels.vwap`: anchored daily, weekly and monthly VWAP with ±1σ and ±2σ bands from
    exact running sums.
  - `levels.engine`: prior-day TPO and VP, prior-week VP, PDH/PDL, PWH/PWL, dPOC, naked
    POCs, VWAPs, ATR(1h), confluence zones (label, score, tolerance).
  - CLI and make targets: `levels --ts`, `footprint --ts --res`, `sanity-plots`.
- Verified (66 tests): hand-computed fixtures for aggregation, delta/CVD with session
  reset, imbalances and stacks, POC tie-breaks, value-area expansion and ties, TPO counts,
  HVN/LVN, VWAP σ, confluence clustering. Aggregation 1m → res equals direct computation.
  **No-lookahead property test**: levels at a random t from the full data equal levels
  from trades truncated at t, which include a partial open minute. This test found the
  non-deterministic float summation fixed in D-017.
- Pending on the VPS: `make sanity-plots` (3 random real days per symbol into
  docs/sanity/) and `make levels TS=...` spot checks against TradingView, after
  `make bootstrap`.
- Open issues: `compute_levels` recomputes everything from frames on every call. Prior-
  session levels are constant within a session and should be cached per (symbol, day)
  before the Phase 3 bar-by-bar backtest; profile this first.
