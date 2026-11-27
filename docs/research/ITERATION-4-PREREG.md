# ITERATION-4 (FINAL) — "TrejdingHub method replica": pre-registration

Owner instruction of 2026-11-27. Written and committed **before** any ITERATION-4 code, data
download or run. Referenced from `docs/DECISIONS.md` row `ITERATION-4`; the parameters below
are mirrored 1:1 in `configs/default.yaml` section `iteration4`.

**This is the last iteration.** If it is killed, `FINAL-KILL.md` stands and no further
iteration is run. The 9-month hold-out (2025-12-28 … 2026-09-27) stays untouched: nothing is
evaluated there, and no aggTrades of hold-out days are downloaded or processed.

## Scope

- Instruments: BTCUSDT, ETHUSDT (Binance USD-M data). Execution costs as before: OKX default
  profile (`okx_eu_inverse`, taker 0.05 % UNCONFIRMED) + 1 footprint bucket slippage per fill.
  Sensitivities: maker entry, OKX 2-tick, OKX 4-tick slippage.
- Windows (as in MASTER-PLAN): **A** = 2024-12-28 … 2025-12-27, **B** = 2026-09-28 … 2026-11-26.
  Report A, B and A+B. Entries only inside A or B; candles before a window are indicator input
  only (40-day kline warm-up as in Phases A/B; for B this lies in the hold-out's calendar span
  and is used as indicator input only, as in Phase 3 and Phase B).
- Candles: Binance USD-M 1m klines (`data/klines`). Orderflow (c2 only): the 1m footprint from
  aggTrades (`data/derived`). Window A's footprint is built from 2024-12-08 (20-day baseline
  warm-up) to 2025-12-27.
- A trade still open at a window end is closed at the last close before the end (as D-039).

## Process (all conditions in this order)

Bars are UTC-aligned aggregates of the 1m klines (15m, 30m, 1h, 4h); a bar is known at its
close. The decision time of a setup is a 15m bar close; nothing after it is used.

### 1. Bias (4H, closed candles only) — grid dimension

The bias is read from the last 4H bar closed at or before the decision time.

- **b1**: EMA50 of 4H closes (`ewm(span=50, adjust=False)`, seeded at the first loaded bar;
  ≥ 240 bars of warm-up). Long bias: `close > EMA50` and `EMA50[t] − EMA50[t−3] > 0`.
  Short bias: `close < EMA50` and `EMA50[t] − EMA50[t−3] < 0`. Otherwise no trade.
- **b2**: 4H market structure from 2-bar fractals. Swing high at bar j: `high[j]` strictly
  greater than the highs of the 2 bars before and the 2 bars after; swing low mirrored. A swing
  is confirmed at the close of bar j+2 (no lookahead). Long bias: the last confirmed swing high
  > the previous confirmed swing high **and** the last confirmed swing low > the previous
  confirmed swing low. Short bias: both lower. Otherwise (mixed, equal, or fewer than 2 swings
  of a kind) no trade.

Trades only in the bias direction.

### 2. POI (static, no lookahead, first test only)

Long POIs (short mirrored):

- **Prior-day TPO** VAL and POC of the session (UTC day), from the prior day's 1m OHLC
  (D-016 conventions: each 30-min period adds 1 TPO to every row between its low and high,
  rows = `tpo_tick`, 70 % value area; same code as Phase B). Eligible for longs only if below
  the session open price (shorts: VAH and POC above it).
- **Prior-week TPO** VAL (shorts: VAH) of the previous ISO week (Mon 00:00 … Mon 00:00 UTC,
  same TPO rules over 7 days). Eligible on a day only if below that day's open price and not
  yet tested since the week start.
- **Fresh demand zones** on H1 and M30 (shorts: supply zones). With bars i−1, i, i+1 of the
  zone timeframe: impulse bar i bullish (`close > open`) and a bullish FVG
  `low[i+1] > high[i−1]`. Base = the single candle i−1. Demand zone = base body-to-wick range
  `[low[i−1], max(open[i−1], close[i−1])]` (supply: `[min(open, close), high]` of the base,
  with a bearish impulse and `high[i+1] < low[i−1]`). The zone exists from the close of bar
  i+1. It expires 168 h after creation. Zones with zero height are dropped.

**First test only.** For each POI, the "near edge" is the edge facing the price (level: the
level itself; demand zone: its top) and the "far edge" is the other one (level: the level;
demand zone: its bottom). Starting from the moment the POI becomes eligible, the 15m bars are
scanned:

- the **test** starts at the first 15m bar whose range reaches the near edge (long:
  `low ≤ near`);
- the **sweep candidate** is the first 15m bar of the test with `low ≤ far − 1 tick`;
- the POI is **consumed** without a setup if, before any sweep candidate, a 15m bar of the
  test closes back beyond the near edge (long: `close > near`), or when it expires, or after
  its sweep candidate (one sweep chance per POI).

### 3. Liquidity sweep (15m)

The sweep candidate C must also trade beyond the most recent **15m swing low** (long; swing
high for shorts) by ≥ 1 tick: `low[C] ≤ swing_low − tick`. The swing is a 2-bar fractal on
15m (same definition as b2), the latest one confirmed at or before C's open. If none exists, no
setup.

Close back on the POI side: if C closes above the far edge (`close > far`), the close-back
bar K = C; otherwise, if the next 15m bar closes above the far edge, K = C+1; otherwise no
setup. The sweep extreme is the lowest low of C … K. 1 tick = the Binance exchange tick
(BTC 0.1, ETH 0.01).

If several POIs produce a setup at the same K, it is one setup (all those POIs are consumed).
The bias (step 1) is evaluated at the decision time; a setup against the bias is dropped.

### 4. Confirmation — grid dimension

- **c1** (candle only), on K: bullish engulfing (K bullish, the previous 15m bar bearish,
  `close[K] ≥ open[K−1]` and `open[K] ≤ close[K−1]`) **or** K closes in the top third of its
  range (`(close − low) / (high − low) ≥ 2/3`, `high > low`). Shorts mirrored. Decision time =
  close of K.
- **c2** (candle and orderflow): c1 **and** the existing `absorption` or `exhaustion` detector
  (direction-matched, current `signals` thresholds, robust baselines over 20 days, slot 30 min)
  fires on a **30m** bar that overlaps the sweep span C … K. To avoid lookahead the decision
  waits for those 30m bars to close: decision time = close of the 30m bar containing K
  (= K's close or 15 min later). Setups whose 30m sweep bars lack footprint or overlap an
  unrepaired data hole are not confirmed (counted in the report).

### 5. Trade

- Entry: open of the 15m bar starting at the decision time (its first 1m open).
- Stop: sweep extreme − `stop_buffer_ticks` (2) OKX execution ticks (shorts: +), never closer
  than `max(0.25 × ATR(1h), 3 × round-trip cost)` from the entry (ATR(1h) = SMA 14 of complete
  hours known at entry; round-trip cost = default scenario, as ITERATION-1/Phase B). If the
  stop is already beyond the entry, the trade is invalid.
- Target: the **nearest** structural level strictly beyond the entry in the trade direction,
  among the entry session's prior-day VAH/VAL/POC, the entry week's prior-week VAH/VAL/POC, and
  the near edges of the opposite zones (H1/M30 supply zones for longs, demand zones for shorts)
  that exist, are unexpired and not yet consumed at the entry. The trade is taken only if
  `target distance ≥ minRR × stop distance` (stop distance after the minimum-stop rule); if
  the nearest level is too close or there is none, the setup is skipped (no further level is
  tried). minRR is a grid dimension {2.0, 3.0}.
- Exits on the 1m path: stop first if the stop and target are touched in the same minute;
  target fills at the target; time stop 24 h (close of the last minute).
- Max 1 trade per symbol per UTC day (entry day) and no new entry while a trade on that symbol
  is open. Setups are processed in decision-time order; a setup skipped for RR, missing data
  or an invalid stop does not use the day's slot.

## Grid, selection, kill

Grid: bias {b1, b2} × confirmation {c1, c2} × minRR {2.0, 3.0} = **8 configurations, no
others**. Cumulative configurations after this iteration: 61 + 8 = **69**.

- Selection: highest net expectancy in R (default costs) on A+B among configurations with
  ≥ 60 trades on A+B; tie → smaller max drawdown.
- **Kill** (→ FINAL-KILL stands): no configuration with ≥ 60 trades, or the selected
  configuration's A+B net expectancy ≤ 0 R, or sign(A) ≠ sign(B).

If it passes: N, net expectancy (R and bps), win rate, PF, max drawdown, monthly stability,
cost sensitivities, uplift c2 vs c1 (same bias and minRR; c2 trades vs c1 trades, and c2 vs
the c1 trades that c2 did not confirm, with a session-clustered bootstrap CI), MDE for the
hold-out, and the cumulative configuration count. Then STOP for the owner's decision on the
hold-out. These numbers are reported whether or not it passes.

`docs/research/TRADES-ITER4.md`: 10 example trades of the selected configuration (or, if no
configuration reaches 60 trades, of the one with the most trades): the 3 best, 3 worst and 4
around the median net R, each as a 15m chart showing the bias, POI(s), the swept swing, the
sweep and confirmation candles, entry, stop, target and exit.

## Data for c2 (window A)

Window A's aggTrades are processed with a new chunked path: day archives processed month by
month, each day's CSV read in batches (default 1M rows) into the same `Ingestor`
(batch-invariant, one code path), raw files deleted after each day, peak memory < 1.5 GB, disk
checked < 70 % before each day. The command refuses any day inside the hold-out.

## Ambiguities decided here (conservative where it matters)

1. "Static at session open": "below/above price" is checked against the day's open; prior-week
   levels additionally must be untested since the week start.
2. The prior-week POC is not a POI (the instruction lists only prior-week VAL/VAH) but it is a
   target level.
3. Zone base = a single candle (the one before the impulse); no minimum FVG size beyond > 0;
   zone lifetime 168 h (the instruction says "fresh" without a number).
4. One sweep chance per POI; the next-bar close-back extends the sweep extreme to the lowest
   low of C … K.
5. c2's decision waits for the 30m bar to close, so a c2 trade can enter up to 15 min later
   than the matching c1 trade.
6. Only the nearest target is tried (no skipping to a farther level to satisfy minRR).
7. Ties in fractals do not form swings (strict inequality).
