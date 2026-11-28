# FLOWLEVELS — specification

Original spec: 2026-11-25. **Amendments of 2026-11-25** (owner decisions A/B/C and the
risk responses) are merged into the text below and marked **[A1]**. **Amendments of
2026-11-26** (owner decisions A/B/C after the Phase 2 sanity review) are marked **[A2]**.
**Amendments of 2026-11-27** (owner decisions 1–4 before Phase 3) are marked **[A3]**. If anything here
conflicts with older notes, this file wins. Every change to this file goes into
`docs/DECISIONS.md`.

## 1. Goal and core hypothesis

The system computes key price levels for BTCUSDT and ETHUSDT perpetuals on Binance USD-M
Futures. It reconstructs orderflow from tick-level trades and detects confirmation signals
at those levels. It runs live on a Hetzner VPS with a web UI, Telegram alerts and an API
that existing trading bots can use as an entry filter. Its job is to show whether
confirmation improves trade outcomes compared with trading the level alone.

Hypothesis: touching a key level alone has roughly no edge. The edge we want to test is
better entry selection and tighter risk. When aggressive flow into a level is absorbed or
exhausted, a reversal entry with a stop just beyond the signal extreme should have better
expectancy than an entry on the bare touch. If the evidence says no, the output is a clear
KILL report.

## 2. Hard rules

See `AGENTS.md`. In short: one code path, no lookahead, exchange timestamps only, correct
aggressor mapping (`m=true` means the aggressor is the SELLER), thresholds in versioned
config, costs always on, typed fail-fast settings, deterministic and reproducible runs.

## 3. Tech stack

- Python 3.12 with uv (lockfile), ruff, mypy (strict on core modules), pytest and hypothesis.
- polars and pyarrow parquet (zstd), duckdb, numpy (numba only if profiling shows a need).
- asyncio and websockets, httpx. SQLite in WAL mode for live signals, events and levels.
- FastAPI with WebSocket push. Frontend: TypeScript, Vite and TradingView
  lightweight-charts; the footprint is drawn on a custom canvas.
- Telegram Bot API. systemd on Ubuntu (Hetzner). CI on GitHub Actions.

## 4. Data

### 4.1 Venue adapter
`VenueAdapter` is an exchange-agnostic interface covering historical download, live
trades, liquidations and REST gap backfill. One implementation for now: Binance USD-M.

### 4.2 Historical data
Source: data.binance.vision, USD-M futures `aggTrades` (plus 1m klines, used only for
validation). Every download is checked against its CHECKSUM file. Processing is streaming,
one day at a time: download → verify → build the 1m footprint and 1m OHLCV → delete the raw
file. Raw ticks are kept only for a few fixture days.

**[A1] History is used for:**
- (a) bootstrapping the last 30 days (time-of-day baselines and level state);
- (b) the **tuning window**: the most recent 60 days, small grid (Phase 3);
- (c) the **hold-out**: the 9 months immediately before the tuning window. It is run **once**
  with frozen thresholds, on the same code path and the same streaming pipeline (raw data is
  deleted after processing). It is never used for any parameter choice. Every re-run after a
  change is logged in DECISIONS.md, and the number of hold-out runs appears in every
  hold-out report.

### 4.3 Live data
- Per symbol, the streams are aggTrade and forceOrder (plus markPrice, used only to display
  funding).
- Gaps: aggTrade IDs are sequential. On a gap or a reconnect, the missing IDs are backfilled
  from REST aggTrades with `fromId`, then the stream resumes. Every gap is logged.
  REST only serves the last 48 h; older gaps are filled from the daily archive (D-022).
- Endpoint: the routed `wss://fstream.binance.com/market` (D-021).
- The collector handles the forced disconnect at maximum connection lifetime, ping/pong,
  and exponential backoff with jitter.
- Watchdog: if BTCUSDT has no trades for more than 60 s, send a Telegram alert and
  reconnect.
- Cold start: run the 30-day bootstrap, then switch to the WebSocket.
- **[A1]** All endpoint and payload assumptions are marked UNVERIFIED in DATA_SOURCES.md
  until `make verify-api` passes on the VPS.

### 4.4 Known limitations
- forceOrder does not deliver every liquidation: it sends only the largest one per symbol
  per 1000 ms (verified 2026-11-26). Liquidation features are indicative only and never enter
  the gate statistics.
- A single venue is a proxy for the whole market. There is no L2 orderbook in the MVP.
- The sub-minute order of trades is dropped once the 1m footprint is built.

### 4.5 Storage and retention
```
data/raw/aggtrades/symbol=S/date=YYYY-MM-DD/part-*.parquet
data/raw/liquidations/symbol=S/date=.../
data/derived/footprint_1m/symbol=S/date=.../
data/derived/ohlcv_1m/symbol=S/date=.../
data/research/runs/<run_id>/...
```
Normalized trade schema: ts_ms int64, trade_id int64, price float64, qty float64,
aggressor 'B'|'S'.
Retention: raw live ticks 7 days; 1m footprint 30 days (kept longer for research runs);
level state is compact and kept forever, including weekly and monthly VWAP running sums
(Σpv, Σv, Σp²v); signals, events and outcomes in SQLite are kept forever. Target disk
usage on the VPS is a few GB.

## 5. Bar resolutions
- Levels: daily (UTC 00:00) and weekly (Monday 00:00 UTC) sessions.
- Context: a 1h/4h regime label with at most 2 parameters.
- Signals: default 5m. The tuning window also evaluates 1m, 3m and 15m and picks the
  primary resolution. Other resolutions are logged in shadow mode.
- Execution detail: the 1m footprint.

## 6. Levels engine
Prior-session levels are computed as of the start of the session. Developing levels are
computed incrementally. No future data is used anywhere.
- TPO (30-minute periods, row size `tpo_tick`): prior-day POC, VAH and VAL (70% value area),
  developing POC, naked POCs (look-back N days), single prints (optional).
- Volume profile: prior-day and prior-week POC, VAH and VAL. HVN and LVN come from a
  smoothed profile using documented peak detection. **[A2]** Nodes are ranked by
  topographic **prominence** (not height), must be at least `hvn_min_distance_k_atr` ×
  ATR(1h, as of the session open) apart, and only the top `hvn_top_n` HVNs and `lvn_top_n`
  LVNs per profile are kept (defaults 3 + 3, D-028).
- VWAP: daily, weekly and monthly anchored, with ±1σ and ±2σ volume-weighted bands.
- Ranges: PDH/PDL and PWH/PWL.
- Confluence: levels within k × ATR(1h) are clustered. Each cluster gets a label and a
  score equal to a weighted count. Zone = cluster price ± tolerance. **[A2]** Clusters are
  **anchored** on their strongest member (members within k × ATR of the anchor, so they
  cannot chain) and a zone is never wider than `zone_max_width_k_atr` × ATR(1h) (default
  0.5, tolerance included, D-029).
- **[A3] Data holes:** recorded holes (D-026) are **repaired** from the raw `trades`
  archive, verified against 1m klines, and the repair is recorded with its source in the
  data-quality metadata (D-032). A hole that cannot be repaired keeps the **[A2]** fallback:
  bars that overlap it, and sessions whose level inputs overlap it, are excluded from
  baselines, level checks and research statistics, and every report states how much was
  excluded (`flowlevels.data.quality`, D-031).
- **[A3] Zones:** static zones are built once at the session open from static levels only
  (prior-day/-week TPO and VP levels, HVN/LVN, PDH/PDL, PWH/PWL, naked POCs) and keep their
  id `symbol:session:anchor` for the whole session. Dynamic levels (dPOC, VWAP d/w/m and
  bands) are not zone members: each is a standalone dynamic zone, reported separately, and
  a static-zone touch records the dynamic levels inside it as a confluence attribute. Every
  touch is assigned to exactly one zone, the one with the nearest anchor (D-034, D-035).

## 7. Orderflow engine
- The base resolution is 1m. Higher resolutions are built by adding up 1m rows per price
  bucket.
- Per bucket: bid volume (seller-aggressor), ask volume (buyer-aggressor), trade count and
  max single trade size.
- Per bar: volume, delta, CVD (reset each session and continuous), bar POC.
- Diagonal imbalances use `imb_ratio` (default 3.0) and a minimum volume. A stacked
  imbalance is at least `stack_n` (default 3) consecutive imbalanced levels.
- Bucket size: BTC 10 USD, ETH 0.5 USD (configurable).
- **[A1]** Normalization baselines are computed per time-of-day slot (30 min) over the
  previous 20 days, strictly from the past, using **median/MAD** (robust z-score), with a
  **weekday/weekend split** (weekday slots use weekday history only, weekend slots use
  weekend history only, see DECISIONS D-006).

## 8. Signal detectors
A detector is a pure function of (footprint history up to t, zone, context) and returns
`Signal | None` with type, direction, strength in [0, 1], features, zone_id and ts. It is
evaluated only while price is inside a zone. Defaults are fixed before any results are
seen.
1. Absorption
2. Exhaustion
3. Delta divergence
4. Stacked imbalance reversal
5. Trapped traders
6. Liquidation spike (informational only)

The confirmation score is a transparent weighted sum of the detectors that fire within W
bars of the touch. No ML.

## 9. Trade model and event groups [A1]
Event groups:
- **Touch**: any touch of a zone in a session (1st, 2nd and 3rd touch tracked separately),
  with entry in the reversal direction.
- **Treatment**: a touch followed by a confirmation score ≥ threshold within W bars.
- **Unconfirmed**: a touch of the same zone types where the score stays below threshold
  within W bars. **The primary comparison is paired: Treatment vs Unconfirmed.** The bare
  "baseline" (all touches) is still reported for information.
- **Random control**: random entries inside zones, matched on count and time-of-day
  distribution, with the stop beyond the zone edge (D-005).

Trade model (the same for every group):
- Entry at the next bar's open after the signal bar closes.
- Stop beyond the signal-bar extreme for treatment, or beyond the zone edge for
  unconfirmed, baseline and random. Add a buffer in ticks. **[A1]** Enforce a configurable
  minimum stop distance.
- Exits are evaluated separately: 1R, 2R, 3R, next zone, and a time stop. Use the 1m path to
  decide between stop and target. When both fall inside one 1m bar, assume the stop.
- Costs: taker fee per side (default 0.05%) plus slippage (default 1 bucket). A maker-entry
  variant is reported as a labelled sensitivity. **[A2]** Signals are sourced from Binance
  USD-M but trades are executed on **OKX perpetuals**, so the default fee profile is OKX's
  standard (non-VIP) futures fees; Binance fees remain an alternative profile
  (`costs.fee_profile`, D-030). Every result records the fee profile.
  **[A3]** The execution instruments are OKX EEA `BTC-USD-SWAP` / `ETH-USD-SWAP` (inverse,
  USDC collateral; R and bps are unaffected, D-033). Default fees 0.02%/0.05% (UNCONFIRMED
  until the owner gives the account's rates), slippage 1 footprint bucket per side; OKX
  2-tick and 4-tick slippage are sensitivities.
- **[A1] Cost geometry:** report costs in R per group. Report every net result **in R and in
  bps per trade**. Add a **sensitivity run** where treatment uses the baseline (zone-edge)
  stop.

Metrics per group, symbol, resolution, detector, level type, context and week: N, win rate,
net expectancy (R and bps), profit factor, average MFE/MAE, max drawdown, R distribution,
and uplift = treatment − unconfirmed with a bootstrap 90% CI (fixed seed).

## 10. Phases

- **Phase 0**: repo skeleton, CI, AGENTS/PROGRESS/docs, settings. Done when CI is green.
- **Phase 1**: VenueAdapter and the Binance adapter, streaming historical pipeline,
  TradeSource (Replay/Live), ID-gap report, bootstrap command. **[A1]** Built and tested on
  fixtures only. The real acceptance (30-day bootstrap, kline match, 1h live run) runs on
  the VPS via `make verify-api`, `make bootstrap`, `make validate-klines` and
  `make live-smoke`.
- **Phase 2**: footprint aggregation, levels engine, confluence zones, a CLI for levels and
  the footprint at any timestamp, sanity PNGs. Fixture tests and no-lookahead property
  tests. **Open a PR to main at the end of Phase 2.**
- **Phase 3**: detectors and research pipeline. Tuning-window grid (max 3 values per key
  parameter, report the number of configurations tried). Choose defaults and the primary
  resolution. Report `data/research/runs/<id>/REPORT.md`. Runs on the VPS.
- **[A1] Phase 3b: hold-out.** Before running it, compute and report the **MDE** for the
  expected N (paired treatment vs unconfirmed, from the dispersion of R seen in the tuning
  window) and state whether the test is adequately powered. Then run the hold-out **once**
  with frozen thresholds. Gate (on the paired comparison, net of costs, in R; bps reported
  alongside):
  - GO: treatment expectancy > 0R, uplift ≥ +0.15R, lower bound of the 90% CI of uplift
    > 0, uplift positive in ≥ 60% of weeks.
  - ITERATE: uplift > 0 but not every criterion is met. List the weaknesses and the changes
    that are justified a priori.
  - KILL: uplift ≤ 0 or treatment expectancy ≤ 0R.

  Deliverable: `HOLDOUT_REPORT.md` with the verdict and the hold-out run count. **STOP.**
- **Phase 4**: web UI (chart, footprint panel, signals, forward test, health). Responsive.
  Playwright screenshots.
- **Phase 5**: live engine and forward-test tracker, Telegram alerts labelled UNVALIDATED,
  parity test (state snapshot included, D-007), 24h soak test.
- **Phase 6**: bot API (`/api/v1/confirmation`, `/levels`, `/health`), Python client with an
  `on_unavailable` policy, `docs/INTEGRATION.md` (shadow mode). **Open a PR to main.**
  **STOP** before deploying.
- **Phase 7**: deployment on the VPS (systemd, localhost binding, Tailscale, log rotation,
  retention, disk alert, daily digest).
- **[A1] Phase 8: live forward test as confirmation.** Its purpose is to confirm live
  slippage and regime drift relative to the hold-out, not to make the primary decision.
  Before it starts, write down in DECISIONS.md:
  1. the "consistent with hold-out" criterion (for example: live paired uplift within the
     hold-out 90% CI, and live treatment expectancy > 0R after measured live slippage);
  2. a minimum N derived from the MDE, not a fixed 100.

  Thresholds stay frozen; any change resets the counter. Report weekly. Deliverable:
  `FORWARD_TEST_REPORT.md`. **STOP.** The "UNVALIDATED" label is removed only with the
  owner's approval.

## 11. Testing
Unit tests cover: aggressor mapping; footprint aggregation against hand-computed fixtures;
TPO/VA/POC including ties and the expansion rule; VWAP bands; imbalances; every detector on
synthetic tapes built to trigger and to not trigger it; trade-model resolution.
Also: no-lookahead property tests, the replay/live parity test, kline validation, the
random-control sanity check, and Playwright smoke tests. CI must be green.

## 12. Out of scope (see BACKLOG.md)
Other venues, paid data, options levels, L2 features, price-action patterns, ML, more
symbols, automatic execution.

## 13. Definition of done (MVP)
- The live system runs on the VPS and the UI is reachable via Tailscale.
- Telegram alerts work. The bot API and client are documented, including shadow mode.
- The hold-out report is delivered. The forward-test tracker is running and its
  confirmation report is delivered.
- Tests are green, and PROGRESS.md and DECISIONS.md are current.
