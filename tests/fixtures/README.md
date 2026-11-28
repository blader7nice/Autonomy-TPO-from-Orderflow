# Test fixtures (REAL data)

These files are real Binance USD-M market data, not synthetic data. Every other test
builds synthetic tapes in memory (`tests/helpers.py`).

| File | Content |
|---|---|
| `btcusdt-2026-11-01_17-ohlcv_1m.parquet` | BTCUSDT 1m OHLCV 2026-11-01 00:00 … 2026-11-17 23:59 UTC. Columns used by the levels engine only: `minute_ts, open, high, low, close, volume, sum_pv, sum_p2v`. |
| `btcusdt-2026-11-01_17-footprint_1h.parquet` | The same period's 1m footprint summed per hour and bucket (`minute_ts` = hour start, `bucket`, `ask_vol`, `bid_vol`). |

Built on 2026-11-26 from the 30-day bootstrap (data.binance.vision aggTrades, checksums
verified, `docs/acceptance/bootstrap-2026-11-26.md`). 2026-11-01 is the earliest minute the
levels at 2026-11-17 depend on (monthly VWAP). When the fixture was built, the levels and
zones at each of the 24 hourly snapshots of 2026-11-17 were checked to be **identical** to
those from the full 1m footprint. Hourly sums are exact there, because profiles only sum
whole sessions and every hourly snapshot sees only closed hours. Do not use the hourly
footprint for sub-hour timestamps. No data hole touches this period (D-031).

Used by `tests/test_levels_real.py` (regression for D-028/D-029).
