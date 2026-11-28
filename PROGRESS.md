# PROGRESS

Current phase: **LIVE PAPER FORWARD TEST running** (installed by the owner on 2026-11-28; ledgers start at the first systemd start). Dashboard fixes + verification layer done 2026-11-28 (D-048, deploy = restart `flowlevels-web`). FINAL-KILL stands. Hold-out runs: 0.

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

## Pre-Phase-3 fixes (owner decisions A/B/C of 2026-11-26) ✅ (2026-11-26)
- **A. Data hole kept, excluded (D-031).** The 2026-10-27 00:02:46–00:30:05 hole is not
  filled. New `flowlevels.data.quality` holds the single exclusion rule: bars that overlap a
  hole, and sessions whose level inputs ([history_start_ms(open), close)) overlap one. The
  sanity-plot picker and the new level statistics exclude them and print how much was
  excluded. In the 30-day window that is **24 of 58 sessions (41.4%)**, 10-28 … 11-08 for both
  symbols (prior-week profile and October monthly VWAP). Baselines and research do not exist
  yet; Phase 3 must use this module (SPEC §6 [A2]). Repair stays in BACKLOG, required before
  the hold-out.
- **B. HVN/LVN and confluence (config v2).**
  - HVN/LVN by topographic prominence (≥ 5% of max), ≥ 0.5 × ATR(1h at session open) apart,
    top 3 + 3 per profile (D-028).
  - Anchored confluence: each cluster is anchored on its strongest member, members lie
    within 0.25 ATR of the anchor, and zones have a hard cap of 0.5 ATR width, tolerance
    included. The tolerance goes from 0.15 to 0.10 ATR so that two levels can still form a
    zone (D-029).
  - Regression test on **real** BTC 2026-11-17 data (`tests/test_levels_real.py`, 0.9 MB
    fixture, `tests/fixtures/README.md`). At 24 hourly snapshots: no zone wider than the cap,
    ≤ 3 + 3 nodes, nodes constant within the session, 15–45 levels, 3–15 confluence zones per
    snapshot (measured: 33 and 7.7).
  - Before/after over the 17 clean sessions per symbol (`docs/sanity/README.md`):

    | | BTC v1 → v2 | ETH v1 → v2 |
    |---|---|---|
    | levels/day | 107.8 → 31.2 | 87.4 → 30.4 |
    | HVN+LVN/day | 82.7 → 6.0 | 62.9 → 5.9 |
    | confluence zones/snapshot | 4.5 → 8.3 | 5.4 → 7.7 |
    | … overlapping the day's range | 2.2 → 5.0 | 2.6 → 4.7 |
    | zone width mean USD / ATR | 860 / 1.41 → 210 / 0.34 | 24.5 / 1.18 → 7.3 / 0.34 |
    | zone width max USD / ATR | 5,911 / 8.69 → 531 / 0.50 | 197 / 7.24 → 19.7 / 0.50 |
  - Sanity plots regenerated (2026-11-09, 11-12, 11-17, 11-20). The v1 plots are kept in
    `docs/sanity/v1/`. New `make levels-stats`.
- **C. Execution on OKX (D-030).** `costs` now holds named fee profiles. The default is
  `okx_regular`: maker 0.02%, taker 0.05%, VERIFIED from OKX's official notices (published
  2026-03-24, effective 2026-04-08; regular tier unchanged by the 2026-09-09 change). The
  alternative is `binance_regular` (same rates, UNVERIFIED). The live `okx.com/fees` table
  is JavaScript-only and was not read directly. **From the German VPS, OKX serves the EEA
  site, which has no futures tab**: check perpetual access and fees for the owner's actual
  account (DATA_SOURCES "OKX fees"). OKX as a second flow source (A/B test) and funding
  costs were added to BACKLOG.
- Tests: 105 passed (was 97), `make check` green.
- Open for the owner (docs/sanity/README.md "Open observations"):
  1. Neighbouring zones can touch or overlap. Phase 3 needs a rule that assigns each touch to
     exactly one zone (proposal: nearest anchor).
  2. Zone ids change when a developing member (VWAP d, dPOC) moves. Touch counting needs a
     stable zone identity.
  3. The strict hole rule excludes 41% of the current 30-day window.

## Pre-Phase-3 decisions 1–4 (owner, 2026-11-27) ✅
- **1. Hole repaired (D-032).** The 2026-10-27 00:02:46–00:30:05 hole was filled from the raw
  `trades` archive using aggTrade fill-id ranges. It is verified against the klines: the 27
  interior minutes match exactly (volume, taker-buy, count), the flipped aggressor matches 0
  minutes, and day totals equal the klines (BTC 102,554.645 / taker-buy 50,355.434). BTC got
  38,304 fills and ETH 48,287. The source file and sha256 are in the completion marker
  (`repairs`). The exclusion logic stays as the fallback for unrepairable holes. **Usable
  sessions after the fill: 120/120 in the tuning window** (BTC 60, ETH 60), and 58/58 in the
  old 30-day window (was 34/58).
- Bootstrap extended to **88 days** (2026-08-31 … 11-26) = 60-day tuning window + warm-up:
  176/176 days, 0 id gaps, 0 unrepaired holes, 13.5 min at nice 10, 2.92 GB streamed,
  footprint 24.7 MB + OHLCV 13.6 MB. `validate-klines` over 88 days: 253,440 minutes;
  mismatches are boundary straddles (D-023) and day edges, plus 46 unexplained ETH minutes
  (0.018%): the two dense chains of 11-16/17 already verified fill by fill, and 13 new
  minutes on 09-11 of the same kind (not individually verified). Reports:
  `docs/acceptance/bootstrap-2026-11-27-88d-repair.md`, `docs/acceptance/klines-2026-11-27-88d.md`.
- **2./3. Zones (D-034, D-035).** Static zones are fixed at the session open (ids
  `symbol:session:anchor@price`). Dynamic levels are standalone dynamic zones, reported
  separately, and a confluence attribute at static touches. Each bar is assigned to one zone
  by nearest anchor.
- **4. Execution (D-033).** OKX EEA `BTC-USD-SWAP`/`ETH-USD-SWAP` (inverse, USDC collateral;
  the `-USDC-SWAP` names do not exist). Specs are in DATA_SOURCES. Fees 0.02%/0.05%
  **UNCONFIRMED**; slippage 1 bucket (owner choice after the 2-tick issue), with 2/4-tick
  sensitivities. R/bps are unaffected by the inverse contract.

## Phase 3 — detectors + 60-day tuning run ✅ in-sample (2026-11-27)
- Code: `signals.features`, `signals.detectors` (5 detectors, liquidations informational),
  `research.{dynamic,touches,trades,stats,report,backtest}`, `make backtest`. 135 tests.
  The selection rule was pre-registered (D-036) and committed before the first run.
- **Run `20261127T115035Z-078b4af7`** (commit 5c3fb39, config 078b4af79f96ba73), window
  2026-09-28 … 11-26. Report: `docs/research/REPORT-20261127T115035Z-078b4af7.md` (full run
  dir incl. trades.parquet on the VPS under `data/research/runs/`). 3 min 44 s, **peak RSS
  2.1 GB** (bots use ~50 MB; 2.9 GB available). One earlier dry run from the uncommitted tree
  (`20261127T113337Z`, same config) gave identical numbers. Its peak RSS was 3.4 GB, which led
  to slice-wise costing and categorical columns.
- **Configurations tried: 36** (4 res × W {2,4,6} × thr {1,2,3}). Selected by the rule:
  **5m, W = 2, threshold = 3**, i.e. all 3 of 5 detectors within 2 bars. It has **N = 41
  treatment trades** vs 3,960 unconfirmed (pooled).
- Static zones, exit 2R, default costs, pooled: treatment **−0.40 R** (−11.7 bps),
  unconfirmed −0.75 R, baseline −0.97 R, random −1.29 R. **Uplift +0.35 R, 90% CI
  [−0.007, +0.711]**, positive in 56% of 9 weeks. In-sample gate preview: 1 of 4 criteria
  (uplift ≥ 0.15). Treatment expectancy is negative in every configuration.
- **Cost geometry dominates**: median stop 15 bps vs ~13 bps round-trip costs, so the
  cost is 0.57 R (treatment) to 1.3 R (random) per trade. Random gross expectancy ≈ 0, which
  is a sound sanity result. Maker entry or OKX tick slippage improve treatment to −0.20 /
  −0.26 R, still negative.
- **MDE for the hold-out: 0.253 R** (SE 0.217 R over 120 sessions, scaled to 548 hold-out
  sessions, one-sided α 0.05, power 80%). **Not adequately powered for the +0.15 R gate**,
  because the selected config fires only ~0.34 times per session.
- Open before Phase 3b (owner):
  1. Review the frozen config / selection. The rule picked the smallest-N config (winner's
     curse). Alternatives with a priori justification must be decided **before** the hold-out.
  2. Power: MDE 0.25 R > 0.15 R gate.
  3. Real fees for the owner's account (placeholder now).
  4. Memory: the run holds all trades in RAM. The 9-month hold-out (~4.5× sessions) needs
     streaming per symbol/resolution before it can run on this VPS.
  5. Bootstrap the 9 months, check for unrepaired holes.

## ITERATION-1 (owner decision 2026-11-27) — KILLED ❌
- Owner review of the Phase 3 run: do not run the hold-out; one pre-registered iteration
  (DECISIONS `ITERATION-1`, commit 4e1f1f0, logged before any code or run). The iteration
  uses confirmation as entry timing for level-to-level trades: first touches, minimum stop
  max(k·ATR, 3 × round-trip cost), target = next static zone ≥ 1.5 R, 48-bar time stop,
  12-config grid, selection = max net R with N ≥ 100, kill if ≤ 0 R.
- Zone-score median frozen from zones only = 1 → the score filter admits all zones (D-037).
- Run `20261127T123132Z-it1-17320d07` (commit fda6ff5): **no configuration reaches 100
  treatment trades (max 10); treatment net expectancy negative in all 12** → **KILL**.
  Cause: the cost-based minimum stop (~40 bps) plus the ≥ 1.5 R target to the next zone
  rejects ~85% of setups in every group. Random control net negative, gross ≈ 0, so there
  is no bug signal. Configurations tried in the tuning window: 48 in total.
- Summary: `docs/research/KILL-ITERATION-1.md`; report `docs/research/REPORT-20261127T123132Z-it1-17320d07.md`.
- Not done, by design: chunked processing, the 9-month download, the hold-out.

## MASTER-PLAN (owner, 2026-11-27) — phases A → D, hold-out untouched
- Plan logged in DECISIONS `MASTER-PLAN` (commit 7fab03b) before any run. It supersedes the
  unrun ITERATION-2 entry (4aa0ecb, 6 configs not counted). Windows A = 2024-12-28 …
  2025-12-27, B = 2026-09-28 … 11-26, hold-out 2025-12-28 … 2026-09-27 never loaded (guard
  in `research.plan`). Candles = Binance USD-M 1m klines (`data/klines`, `flowlevels klines-fill`).
- Code commit 29878f1: kline store, Supply Zone port (`strategies.supply_zone`, D-038:
  TP1 → break-even at the next hourly tick, settled by the bot's code), fidelity tools,
  Dalton H1/H2 (`strategies.dalton`), runners (`flowlevels plan-run`). 153 tests.

### Phase B — ITERATION-3 Dalton ❌ both KILLED
- Run `20261127T140221Z-planb-52a9cce4` (11 s, 0.36 GB). 12 configs (H1 4, H2 8); all
  negative on A+B (−0.085 … −0.333 R). A is near zero, B clearly negative.
- H1 selected `k0.5|none`: N 246, A −0.033, B −0.506, A+B **−0.085 R** → KILLED.
- H2 selected `k0.5|none|r1.5`: N 300, A −0.045, B −0.368, A+B **−0.098 R** → KILLED.
  POC as secondary exit is worse (−0.189 R). Maker entry brings A slightly positive (+0.03 / +0.05)
  but A+B stays ≤ 0.
- Report: `docs/research/PLAN-B-20261127T140221Z-planb-52a9cce4.md`.

### Phase A — ITERATION-2 Supply Zone ❌ KILLED (strategy test; fidelity passed)
- Fidelity (`docs/research/SZ-FIDELITY-2026-11-27.md`): all 34 log assets on Binance klines,
  candles only. Regime agreement 97.4 %, core-signal agreement 98.96 %, logged positives
  reproduced 131/199 (65.8 %), so the pre-registered thresholds pass. Port vs the owner's original pandas
  code on identical frames: 100 % regime, 100 % reasons, 0 level mismatches. The gaps vs the
  log are venue (Hyperliquid / Binance testnet vs Binance mainnet: median 5.5 bps price difference, plus volume).
- Strategy test, run `20261127T140449Z-plana-52a9cce4` (53 s, 0.30 GB): **2 trades in 425
  days** on BTC+ETH (A 1, B 1), both longs stopped out: A −1.097, B −1.217, A+B −1.157 R →
  KILLED by rule. The filters (volume spike, extremes, EMA distance) and the final RR ≥ 3 gate
  remove almost every core signal. This matches the live log (0 BTC/ETH entries).
- Report: `docs/research/PLAN-A-20261127T140449Z-plana-52a9cce4.md`. D-038, D-039, D-040.

### Phases C / C2 — not applicable; Phase D — no candidate ❌ FINAL-KILL
- No rule survived A or B, so the overlay (C) and combinations (C2) had no inputs. The chunked aggTrade
  processing for window A (only needed for C) was not built. D-041.
- `docs/research/FINAL-KILL.md`: all iterations, **61 in-sample configurations**, lessons.
- STOP. The hold-out was never run.

## ITERATION-4 (FINAL, owner 2026-11-27) — "TrejdingHub method replica" ❌ KILLED → FINAL-KILL stands
- Pre-registration `docs/research/ITERATION-4-PREREG.md` (dfe4b4d), committed before any code or run.
- Chunked aggTrade processing (7b5ed67): streamed zip line batches into the same Ingestor,
  month order, disk guard, hold-out refusal. Window-A footprint built: 770 symbol-days, 0 missing
  (D-042; one inconsistent raw-trades repair archive on the 2025-08-29 outage → hole kept
  unrepaired; peak RSS 2.0 GB in the repair step only, over the 1.5 GB budget, logged).
- Method code `strategies.method4`, runner `flowlevels iter4-run` (38526a7). 167 tests.
- Run `20261127T182244Z-iter4-0610dfbc`, once: no configuration ≥ 60 trades (max 40, `b1|c1|rr2`: A+B −0.232 R,
  A −0.265, B +0.173). 7 of 8 configs negative. D-043.
- Reports: `docs/research/ITER4-20261127T182244Z-iter4-0610dfbc.md`, `docs/research/TRADES-ITER4.md` (10 charts).
- Cumulative in-sample configurations: **69**. STOP. The hold-out was never run.

## ITERATION-5 (FINAL, binding hold-out, owner 2026-11-27) — revised method ❌ KILLED at the in-sample gate
- Pre-registration `docs/research/ITERATION-5-PREREG.md` (671e0f7), before any code, download or run.
  Rule changes chosen after seeing the ITERATION-4 charts → the hold-out was to be the binding verdict.
  Owner decision: B's 150-day warm-up uses hold-out-span candles as indicator input only.
- Code `strategies.method5` (T1/T2 triggers, far-edge stop, 50/50 split exit with level/trail),
  `research.iter5`, `flowlevels iter5-run` (c18f07d). 178 tests.
- Data/config (55923c8, D-044): klines for 6 symbols, 0 missing minutes; buckets SOL 0.03, BNB 0.11,
  XRP 0.0004, DOGE 0.00003; OKX EEA instruments (BNB only linear); config v4.
- Run `20261127T194620Z-iter5-da431f34`, once: all 8 configs ≥ 100 trades and negative on A+B. Selected `T2|k0.5|trail`:
  N 218, A −0.116, B +0.127, A+B −0.079 R, 2/6 symbols positive → KILLED (D-045).
- Reports: `docs/research/ITER5-20261127T194620Z-iter5-da431f34.md`, `docs/research/TRADES-ITER5.md` (10 charts).
- Cumulative in-sample configurations: **77**. STOP. The hold-out was never run.

## LIVE PAPER FORWARD TEST (owner, 2026-11-27) — ✅ built + smoke-tested, ⏳ owner install
No real orders and no exchange keys. The plan was logged before any code
(DECISIONS `PAPER-PLAN`, 404a019). The decision rules are fixed in `docs/PAPER_TEST.md`.

Variants, rules frozen per version (rules hash):
- a `T2|k0.5|trail` `5f187deaebad`
- b `T2|k0.5|lvl` `4212db816bd3`
- c `T2|wick|trail` `8d6c2744357c`
- d `T1|k0.5|lvl` `b4d08eeff602`

Each variant trades BTC, ETH, SOL, BNB, XRP and DOGE and has its own ledger.

Steps:
1. **Strategy split** into `prepare` + `trades` (ITERATION-5 output identical), wick stop for
   variant c, signal rows, config v5 (2bf76dc).
2. **Variant c reference backtest**, run once on A+B: N 631, **−0.221 R** (−14.0 bps).
   Variants a, b and d are identical to ITERATION-5. **Cumulative configurations: 78** (D-046,
   d9c63fa).
3. **Paper engine** (5b670b4):
   - REST 1m kline feed + live store; the research code path recomputes every minute.
   - SQLite ledger: signals, trades, chart payloads and PNGs.
   - Divergence guard; Telegram outbox; stall, disk and weekly-summary checks.
   - Restart-safe with REST backfill (trades found that way are marked LATE).
4. **Dashboard** (1ecb318):
   - FastAPI read-only API + WebSocket, bound to 127.0.0.1.
   - Lightweight-charts terminal UI: one `theme.css`, dark, tabular numerals.
   - Pages: Chart, Positions, Trades plus a trade sheet, Performance, Health.
   - DEMO database = window B replayed through the engine (153/153 trades equal).
   - Playwright design QA at 1440 / 390 px over several fix rounds; final screenshots in
     `docs/screens/`.
5. **Deploy** (76d7ccb, 346bcbd):
   - systemd units `deploy/systemd/flowlevels-{paper,web}.service`: user flow, caps, sandboxed.
   - `docs/DEPLOY.md`: .env, units, Tailscale serve, phone.
   - `flowlevels telegram-test`.
   - "How to read the dashboard" in `docs/PAPER_TEST.md`.
   - Stale-snapshot re-poll (D-047).
6. **1-hour live smoke run** (`docs/paper/SMOKE-20261127.md`):
   - Status `running` in 242 of 242 samples.
   - Lag median 39 s, p95 66 s, max 71 s (up to ~120 s before the D-047 fix).
   - Engine RSS ~190 MB (peak 233 MB with a large backfill); web ~95 MB; disk 45 %.
   - No errors. 0 signals in the hour (expected).
   - Restarts resumed cleanly with the same versions.
   - Telegram not tested live: the token is not set yet.

193 tests. Cumulative in-sample configurations: **78**. Hold-out runs: **0**.
STOP for the owner's install.

### Dashboard fixes + verification layer (owner, 2026-11-28) — ✅ (D-048)
Web only: no strategy, engine or ledger change, rule versions unchanged.
- Price axis: no more overlap. The axis shows only tick prices. TPO levels, EMAs, positions
  and the last price are right-edge pills inside the plot (merged or offset with leaders).
- The OHLC readout moved above the chart, so POI labels are no longer covered.
- EMA lines run to the newest candle (the value in force since the last close). An off-scale
  daily EMA50 is pinned to the chart edge with ↑/↓, and the readout says "above/below chart".
- Verification layer:
  - Crosshair readout with the exact 4H/D EMA50, session VWAP and prior-day/week TPO levels
    per bar.
  - `flowlevels.paper.verify`: an independent re-implementation, compared with the engine and
    with Binance REST.
  - Health "Independent cross-check" card (every 15 min, Telegram on fail) and the
    `flowlevels verify` CLI.
  - First live run: PASS on all 6 symbols, max relative difference 1.8e-15.
- 205 tests (12 new). The `docs/screens/` screenshots were refreshed.
