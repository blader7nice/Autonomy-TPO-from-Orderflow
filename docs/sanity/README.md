# Sanity plots (real data)

Regenerated on the VPS on 2026-11-26 with `make sanity-plots` (config **v2**, seed 42, 3 random
clean days per symbol plus 2026-11-17, which is fixed for comparison). Source:
data.binance.vision aggTrades → 1m OHLCV/footprint from the 30-day bootstrap
2026-10-27 … 2026-11-25 (D-019: only real data in this folder).

- `*.png`: config v2 (D-028 HVN/LVN, D-029 confluence): 2026-11-09, 11-12, 11-17, 11-20.
- `v1/*.png`: the previous plots, config v1 (height-based HVN/LVN, single-linkage
  confluence), for before/after comparison. Compare `v1/BTCUSDT-2026-11-17.png` with
  `BTCUSDT-2026-11-17.png`.
- `levels-stats-v1-2026-11-26.md` / `levels-stats-v2-2026-11-26.md`: before/after statistics
  over **all** clean sessions (`make levels-stats`; definitions in `flowlevels.levels.stats`).
  "v1" was produced by running the same statistics module on the v1 code (commit 6f07536)
  in a temporary worktree.

Each PNG shows one UTC day of 15m candles with:
- the **prior-session levels as of 00:00 UTC** (horizontal lines, labelled on the right;
  weekly/monthly VWAP lines are not drawn, only used in zones);
- **confluence zones** with ≥ 2 members (yellow bands; where two zones overlap the band is
  darker);
- the **developing daily VWAP** with ±1σ (dashed) and ±2σ (dotted) bands, each point computed
  from closed 1m bars only;
- data holes recorded by the pipeline (grey), if any.

## Data excluded (D-031)

The archive hole 2026-10-27 00:02:46–00:30:05 UTC (BTC and ETH, 54.6 min in total) is not
repaired (owner decision A). Sessions whose level inputs overlap it are excluded from the
picker and from the statistics: **24 of 58 sessions (41.4%)**, 2026-10-28 … 2026-11-08 for both
symbols (prior-day profile, prior-week profile 10-26 … 11-01, the October monthly VWAP,
nPOCs). That is why the 2026-10-28 and 10-31 plots of v1 were not regenerated. Statistics
below: 17 clean sessions per symbol (2026-11-09 … 11-25).

## Before / after (2026-11-26)

Per session: levels at 00:00 UTC; zones at the 24 hourly snapshots; widths = high − low
of confluence zones (≥ 2 members), tolerance included.

| | BTC v1 | BTC v2 | ETH v1 | ETH v2 |
|---|---|---|---|---|
| levels per day (00:00) | 107.8 | **31.2** | 87.4 | **30.4** |
| of which HVN + LVN | 82.7 | **6.0** | 62.9 | **5.9** |
| zones per snapshot (all) | 15.1 | 23.1 | 16.5 | 23.3 |
| confluence zones per snapshot | 4.5 | 8.3 | 5.4 | 7.7 |
| confluence zones overlapping the day's range | 2.2 | 5.0 | 2.6 | 4.7 |
| zone width mean, USD | 859.7 | **210.4** | 24.49 | **7.34** |
| zone width max, USD | 5,910.7 | **531.0** | 197.38 | **19.65** |
| zone width mean, ATR | 1.41 | **0.34** | 1.18 | **0.34** |
| zone width max, ATR | 8.69 | **0.50** | 7.24 | **0.50** |

BTC 2026-11-17 alone: v1 had 80 levels (53 HVN+LVN) and 2.7 confluence zones per snapshot,
max 1,843 USD (3.9 ATR). v2 has 33 levels (6 nodes) and 7.7 zones per snapshot, max 314 USD
(0.50 ATR). The same day is the regression fixture (`tests/test_levels_real.py`).

## Review notes

What looks right (unchanged from v1):
- Candles match the exchange (validated against 1m klines, `docs/acceptance/klines-2026-11-26.md`).
- The daily VWAP and its bands behave as expected: σ widens after impulsive moves and VWAP
  lags price.
- PDH/PDL, the prior-day value area and POCs sit at plausible prices.

What changed:
- **HVN/LVN**: at most 3 + 3 per prior day, at least 0.5 ATR apart, chosen by prominence. On
  the plots they sit on the visible bulges and gaps of the prior day's profile instead of
  every 20–40 USD.
- **Zones**: no more chaining. Every zone is ≤ 0.5 ATR wide (verified on all 34 clean
  sessions). The "one zone over yesterday's whole range" case is gone. There are more, smaller
  zones per snapshot (8 vs 4.5), and about 5 of them are inside the day's range.

Open observations for the owner (not changed, since they are not covered by the decision):
1. **Neighbouring zones can touch or overlap** (for example BTC 11-17 around 64,200–64,560,
   ETH 11-12 around 2,042–2,052). The width cap splits a dense area into adjacent zones.
   Phase 3 must assign each touch to exactly one zone (proposal: the zone whose anchor is
   nearest) so that touches are not double-counted.
2. Zone ids include the zone price, so a zone with a developing member (VWAP d, dPOC) gets a
   new id whenever that member moves. Phase 3 touch tracking ("1st/2nd/3rd touch") needs a
   stable zone identity, for example the anchor plus prior-session members.
3. The 24 excluded sessions are 41% of the 30-day window. That matters for Phase 3 baselines
   (20 days of history) only until the bootstrap window moves past 2026-11-08, or until the
   hole is repaired (BACKLOG, required before the hold-out).
