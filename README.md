# FLOWLEVELS

Orderflow confirmation at key levels (TPO / volume profile / VWAP / prior ranges) for
BTCUSDT and ETHUSDT perpetuals on Binance USD-M Futures.

The research question: does an orderflow confirmation at a level (absorption, exhaustion,
imbalances, delta divergence) improve trade outcomes compared with an unconfirmed touch of
the same level? The answer comes from a historical out-of-sample hold-out (primary
decision) and is then confirmed by a live forward test. See `docs/SPEC.md`.

Start here: `AGENTS.md` (hard rules), `PROGRESS.md` (status), `docs/SPEC.md` (design).

## Development

```bash
uv sync                 # install pinned env (uv.lock)
make check              # ruff + mypy + pytest
uv run flowlevels info  # version, commit, config hash
```

Everything in this repo is built and tested against recorded/synthetic fixtures. Real
Binance data is only processed on the VPS (see below).

## Running on the VPS

Real-data steps run on the Hetzner VPS (EU; Binance futures blocks some regions). Run them
in this order, from the repository root, after `cp .env.example .env` and editing it.

| Step | Command | What it does | Phase |
|---|---|---|---|
| 0 | `uv sync --frozen` | Install the pinned environment | 0 |
| 1 | `make verify-api` | Probes the real Binance REST/WS/data.binance.vision endpoints and checks every assumption marked UNVERIFIED in `docs/DATA_SOURCES.md`. **Must pass before anything else.** | 1 |
| 2 | `make bootstrap` | Downloads the last 30 days of aggTrades for BTCUSDT and ETHUSDT (with checksum verification), builds 1m footprint and 1m OHLCV, deletes raw files, prints the ID-gap report, disk use and timing | 1 |
| 3 | `make validate-klines` | Compares 1m OHLCV rebuilt from aggTrades with Binance 1m klines for the bootstrapped days | 1 |
| 4 | `make live-smoke` | Runs the live collector for 1 hour (default), with gap backfill, and writes a gap/reconnect report | 1 |
| 5 | `make levels TS=2026-11-20T14:00` | Prints levels, zones and the footprint of the bar at a timestamp | 2 |
| 6 | `make sanity-plots` | Writes PNGs of 3 random clean days (plus 2026-11-17) with levels and VWAP bands to `docs/sanity/` | 2 |
| 7 | `make levels-stats` | Levels/zones per day and zone widths over all clean sessions, to `docs/sanity/levels-stats-<date>.md` | 2 |
| 8 | `make bootstrap DAYS=88` | Extends history to the 60-day tuning window plus ~4 weeks of warm-up (levels, baselines); repairs archive holes from the raw trades archive (D-032) | 3 |
| 9 | `make backtest` | 60-day tuning run: 36-config grid, in-sample `data/research/runs/<id>/REPORT.md` (copied to `docs/research/`). Never touches the hold-out | 3 |
| 10 | `make holdout` | **Single** hold-out run on the 9 months before the tuning window, frozen thresholds | 3b |

Environment variables are documented in `.env.example`. Variables can be overridden per
command, for example `make bootstrap DAYS=7 SYMBOLS=BTCUSDT`.

The commands for later phases are added here as each phase is completed.

## Test fixtures

All files under `tests/` are synthetic tapes built by `tests/helpers.py` in the
(UNVERIFIED) Binance formats. They are not market data and are used only to test parsing,
aggregation, gap handling and the live state machine.
