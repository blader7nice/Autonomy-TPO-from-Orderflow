# Sanity plots (real data)

Generated on the VPS on 2026-11-26 with `make sanity-plots` (seed 42, 3 random complete days,
picked from the 30-day bootstrap 2026-10-27 … 2026-11-25), code as of the Phase 1 acceptance
commit, config `configs/default.yaml` v1. Source: data.binance.vision aggTrades → 1m
OHLCV/footprint (D-019: only real data in this folder).

Each PNG shows one UTC day of 15m candles with:
- the **prior-session levels as of 00:00 UTC** (horizontal lines, labelled on the right;
  weekly/monthly VWAP lines are not drawn, only used in zones);
- **confluence zones** with ≥ 2 members (yellow bands);
- the **developing daily VWAP** with ±1σ (dashed) and ±2σ (dotted) bands, each point computed
  from closed 1m bars only;
- data holes recorded by the pipeline (grey), if any. None of these 3 days has one; the only
  hole in the 30 days is 2026-10-27 00:02–00:30 UTC (D-026).

## Review notes (2026-11-26)

What looks right:
- Candles match the exchange (validated against 1m klines, `docs/acceptance/klines-2026-11-26.md`).
- The daily VWAP and its bands behave as expected: σ widens after impulsive moves (BTC 10-28
  08:30, 11-17 15:30) and VWAP lags price.
- PDH/PDL, the prior-day value area and POCs sit at plausible prices.

**Problem found: HVN/LVN are far too dense, and zones chain into one giant zone.**
- The prior day alone yields ~25–30 HVNs and as many LVNs (BTC 2026-11-17: 28 HVN + 27 LVN), about
  one every 20–40 USD.
- Cause, with the v1 defaults: the volume profile is smoothed over only `hvn_smoothing_rows` = 5
  rows × 10 USD = 50 USD, and a peak qualifies when its height is ≥ `hvn_prominence` = 10% of the
  max. Almost every local wiggle of a day's profile passes that test. (It is a height test, not a
  true prominence test.)
- Effect: confluence clustering is single-linkage within `confluence_k_atr` × ATR. It chains these
  levels together with PDL, VAL, vPOC and the VWAP bands into **one zone covering most of the
  prior-day range**. BTC 2026-11-17 12:00: score 71, 63,804–65,552 (1,750 USD ≈ 3.9 × ATR). With
  zones like that, "touch of a zone" means "price is inside yesterday's range", so Phase 3 would
  test nothing.

Proposed fix, to decide before Phase 3. It is a level-definition change, chosen before any
outcome is seen:
1. HVN/LVN: use true peak prominence (height above the higher neighbouring trough) relative to
   the max, smooth over a width tied to the day's range or ATR (not a fixed row count), and keep
   at most the top N (e.g. 3) HVNs and the LVNs between them.
2. Confluence: cap the zone width (for example ≤ 0.5 × ATR) or use complete-linkage, so clusters
   cannot chain.
3. Optionally leave HVN/LVN out of the confluence score until they are calibrated.
