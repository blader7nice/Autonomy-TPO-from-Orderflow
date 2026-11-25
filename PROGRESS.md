# PROGRESS

Current phase: **2 — footprint + levels** (next)

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
- **Pending on the VPS (acceptance):**
  `make verify-api` → `make bootstrap` → `make validate-klines` → `make live-smoke`.
  Record the results here: per-day timing, disk use, gap report, kline residuals, the
  1h live gap/reconnect log. Then update DATA_SOURCES.md from UNVERIFIED to VERIFIED.
- Open issues / risks:
  - Every WS/REST payload assumption is UNVERIFIED (D-009).
  - Possible kline residuals to investigate on real data: trades in klines that are not
    in aggTrades (for example RPI or special matches), if any. Not verified.
