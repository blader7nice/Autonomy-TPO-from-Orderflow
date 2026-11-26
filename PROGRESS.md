# PROGRESS

Current phase: **Phase 1/2 VPS acceptance done (2026-11-26). STOP: owner reviews the sanity plots and the HVN/zone issue before Phase 3.**

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
- **Step 2: `make bootstrap` ✅ (2026-11-26).** 30 days (2026-10-27 … 2026-11-25), BTCUSDT and
  ETHUSDT: 60/60 days done, **0 in-day gaps, 0 cross-day gaps**, all checksums OK.
  99.9M aggTrades (BTC 54.2M, max 3.74M per day; ETH 45.7M). Wall time 5 min 08 s:
  download+verify 198 s, processing 109 s (max 4.2 s per day), at `nice 10` on 2 vCPU.
  1.27 GB raw downloaded and streamed (deleted per day). Disk: peak data dir 51 MB (one raw
  zip in flight), final **14 MB** (footprint_1m 9.4 MB, ohlcv_1m 4.6 MB). Root fs stayed at 41%.
  Peak RSS 1.07 GB. Report: `docs/acceptance/bootstrap-2026-11-26.md`. The run was on
  133acd2 plus the uncommitted timing-split change of this commit (hence `-dirty` in the report).
  - Also: `LiveSource` now drains the socket in a reader task during long backfills (D-025),
    and kline validation classifies boundary-straddle residuals (D-023).
- **Step 3: `make validate-klines` ✅ (2026-11-26).** 86,346 minutes compared (2 symbols × 30
  days). The 1e-6 mismatch rate is **14.28%** of minutes (BTC 13.6%, ETH 15.0%), and **all of it is
  explained**:
  - 12,247 minutes: aggTrades that straddle a minute boundary. `T` is the first fill's time and
    the other fills come 0–100 ms later; klines bucket each fill. The mismatches net to zero
    across adjacent minutes, chains included (D-023).
  - 43 minutes: the same at a UTC day edge (00:00/23:59). Daily volumes pair up exactly with
    the neighbouring day.
  - 5 minutes: the partial edges of the data hole below.
  - 33 ETH minutes in two dense chains, which the classifier cannot net automatically. They
    were verified fill by fill against the raw `trades` archive: bucketing the fills by their own
    time reproduces the klines exactly.
  - 30-day volume and taker-buy volume equal the klines **exactly** except on 2026-10-27.
    Fill count (Σ l−f+1) is not comparable to kline `count` (ranges include non-market ids).
  - **Surprise, a data hole:** the aggTrades archive (daily *and* monthly) has **no trades on
    2026-10-27 00:02:46–00:30:05 UTC** for BTC and ETH (54 kline-only minutes; −17.3 BTC,
    −1014 ETH). The aggTrade ids are contiguous across it, so id-gap detection was blind. It is
    now detected as a time hole (`data_quality.max_trade_silence_s`, D-026) and listed in the
    bootstrap report. It is not repaired; the raw `trades` archive is complete there (BACKLOG,
    owner decision).
  - The bootstrap was re-run to record holes (footprint format v2): 5 min 05 s, same results.
  - Reports: `docs/acceptance/klines-2026-11-26.md`, `docs/acceptance/bootstrap-2026-11-26.md`.
- **Step 4: `make live-smoke` ✅ (2026-11-26), 2 runs of 1 h.**
  - **Run 1 (13:48–14:48 UTC, cold start, code at step 3).** The resume from bootstrap
    (2026-11-25 23:59:59) backfilled today via REST: BTC 817,011 ids in 17.5 min and ETH
    563,332 in 12.2 min. All resolved, with no 429s and no disconnect during backfill (reader
    task, D-025). **Two watchdog reconnects:**
    (a) 14:18:40, a bug: draining the ~30 min backlog after the backfills starved the reader
    task (`await queue.get()` does not yield when items are waiting). The benchmark also showed
    the consumer at **~10 ms per single-frame batch (~100 frames/s)**, below burst rates.
    Fixed by micro-batching plus a yield per step: 3,400 frames/s (D-027), with regression tests.
    (b) 14:38:07: BTC and ETH both silent for 70 s in a quiet market with no backlog. This was
    a server-side stall; the watchdog worked as designed. Both reconnect gaps were backfilled
    (14,410/11,241 and 1,296/1,367 ids). 1.46M trades, 0 exceptions, peak RSS 381 MB, 36% CPU
    (backfill-heavy).
  - **Run 2 (14:52–15:52 UTC, final code).** Restart resume gap 5,680 BTC / 4,835 ETH
    backfilled in 13 s. Then **0 reconnects, 0 watchdog, 0 gaps, 0 exceptions or warnings**.
    269k trades, 22% of one CPU, peak RSS 516 MB, data dir +2 MB.
  - **End to end:** today's stored raw ticks from 00:00:00 to 15:52:42 are id-contiguous (0
    holes), starting at bootstrap last id + 1, through the cold start, reconnects and the restart.
    Max silence 5.3 s (BTC) / 4.7 s (ETH). Liquidations captured: 169 BTC, 141 ETH.
  - No ping-related disconnects in 2 h (server pings every 3 min are answered by `websockets`).
  - Reports: `docs/acceptance/live-smoke-run{1,2}-*.md`.
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
- **`make sanity-plots` ✅ (2026-11-26):** 3 random real days (2026-10-28, 10-31, 11-17; seed
  42) × BTC/ETH in `docs/sanity/`, with review notes in `docs/sanity/README.md`. Only complete
  archive days are eligible, and data holes are shaded grey. Candles, daily VWAP and the ±1σ/±2σ
  bands, PDH/PDL and value area look right.
  **Problem found, to fix before Phase 3:** HVN/LVN are far too dense (BTC 2026-11-17: 28 HVN +
  27 LVN from one day) because of a height test (≥10% of max) on a profile smoothed over only
  50 USD. Single-linkage confluence then chains everything into one zone covering most of the
  prior-day range (score 71, 1,750 USD ≈ 3.9 ATR). The proposed fix is in the README. It is not
  applied: it is the owner's review item.
- Still open: `make levels TS=...` spot checks against TradingView (manual, owner).
- Open issues: `compute_levels` recomputes everything from frames on every call. Prior-
  session levels are constant within a session and should be cached per (symbol, day)
  before the Phase 3 bar-by-bar backtest; profile this first.
