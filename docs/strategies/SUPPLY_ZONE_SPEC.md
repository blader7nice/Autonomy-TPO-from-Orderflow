# Supply Zone strategy — specification (reverse-engineered from the owner's code)

Source (read-only, not committed): `/home/flow/import/supply_zone/`:
`supply_zone_short.py` (identical to `framework/strategies/supply_zone_short.py`),
`framework/{volume_profile,fvg_detector}.py`, the live bot package `supply_zone_bot/`
(`engine.py`, `signal_checker.py`, `executor.py`, `position_tracker.py`, `config.py`) and
`test_supply_zone_bot.py`. Written 2026-11-27. Where the README and the code disagree, the
**code is authoritative** (the README parameters are outdated: e.g. `atr_sl_mult 0.5`,
`zone_tolerance_pct 0.3`, `zone_strength_min 2`; the code has 1.5, 0.5, 0).

The live bot ran on **Hyperliquid** (`BTC/USDC:USDC` etc., 24 instruments), hourly.

## 1. Timeframes and data the live bot sees

- Scan once per new 1h bucket (`_tick`), in practice at HH:00–HH:06 (median HH:03:14 in the
  BTC/ETH log).
- Candles via ccxt `fetch_ohlcv`: the **last 300 1h** and **last 150 4h** bars, **including the
  still-forming bar** (the staleness guard accepts a last bar up to 90 min old). At HH:03 the
  last 1h row covers ~3 minutes; the last 4h row covers 0–4 h.
- Minimum data: 100 1h bars and 50 4h bars.

## 2. Regime filter (4h) → direction

On the 150-bar 4h frame (incl. the forming bar): EMA20, EMA50 (`ewm(span, adjust=False)`,
so they depend on the window start), slope = `EMA50.pct_change(10) / 10 × 100` (% per bar).
Threshold 0.05 for BTC/ETH (0.08 for most alts).

- `BULL`: EMA20 > EMA50 and slope > +thr → **SHORT** only (mean-reversion rejection from supply).
- `BEAR`: EMA20 < EMA50 and slope < −thr → **LONG** only (mean-reversion bounce from demand).
- otherwise `CONSOLIDATION` → no trade.

The strategy is **counter-regime**: it shorts in up-trends and buys in down-trends.

## 3. Core signal (`SupplyZoneShortStrategy.generate_signals`, run on the 300-bar 1h frame)

Indicators on the 1h frame: ATR(14) = SMA of true range; RSI(14) = SMA-based (rolling means
of gains/losses).

Context is **recalculated every 50 bars** (`RECALC_INTERVAL = 50`) starting at bar index 1,
i.e. at indices 1, 51, …, 251. The bot only reads the **last row (index 299)**, so the zones,
VP and FVGs used for the live decision are those computed at **index 251, ~48 hours before
the scan**:

- **Zones (4h)**: 4h bars with open time ≤ open time of 1h bar 251; take the last 50; ATR(14)
  (SMA) on that 50-bar slice. Supply zone = a bearish 4h candle with body > 1.2 × ATR:
  `top = open, bottom = close`. Demand zone = a bullish one: `bottom = open, top = close`.
  Strength = later bars that reach and close back out of the zone (supply: `high ≥ bottom` and
  `close < bottom`; demand: `low ≤ top` and `close > top`). Zones are sorted by strength
  (desc, stable); `zone_strength_min` is unused.
- **Volume profile (1h)**: bars 151..251 (101 bars), 50 equal-width bins between min low and
  max high, each candle's volume spread uniformly over its range; POC = centre of the max bin.
- **FVGs (1h)**: bars 201..251; bearish FVG: `low[i-2] > high[i]`, gap = (high[i], low[i-2]);
  bullish: `high[i-2] < low[i]`; size ≥ 0.1% of mid. Active if age ≤ 50 bars (relative to
  bar 251) and fill < 80% measured against the close of bar 251.

Decision on the last row (price = its close = the forming bar's last price):

- **in zone**: the first zone (in strength order) with
  `bottom − 0.5% × price ≤ price ≤ top + 0.5% × price`.
- confluence = in zone (1) + POC within 1.5% of the zone mid (1) + price inside an active FVG
  of the matching type (1). The gate `confluence ≥ 1` is implied by being in a zone.
- SHORT: in supply zone and (forming bar bearish `open > close` **or** RSI > 55).
  LONG: in demand zone and (forming bar bullish `close > open` **or** RSI < 45).
- Strategy SL/TP (coarse, only used for the RR gate):
  SHORT `SL = top + 1.5 ATR`, `TP = bottom − 3.0 ATR`;
  LONG `SL = bottom − 1.5 ATR`, `TP = top + 6.0 ATR` (**asymmetric**: 2 × atr_tp_mult);
  require reward/risk ≥ 1.5 (and again in `validate_signal`).

## 4. Entry filters (signal_checker, after the core signal, on the same 1h frame)

1. Volume spike: `max(vol[-1], vol[-2]) / mean(vol[-21:-1]) ≥ 1.5`. Note `vol[-1]` is the
   forming bar (a few minutes), so in practice the previous complete hour decides.
2. Local extreme: LONG: `min(low[-3:]) ≤ min(low[-20:])` (a fresh 20-bar low within the last 3
   bars, forming bar included); SHORT mirrored with highs.
3. Multiday extreme: the same with a 100-bar lookback.
4. EMA distance: `|close − EMA20(close)| / ATR14(1h) ≥ 3.0`.

## 5. Stops, targets, final gate (engine/executor, live path)

ATR for SL/TP = ATR(14) SMA on the **4h** frame (incl. forming bar).

- SL: SHORT `max(zone_top + 0.3 ATR, max(high of last 20 4h bars) + 0.2 ATR)`; LONG mirrored
  with `min`. Rejected if on the wrong side of entry.
- TP1 = **zone midpoint** (33% of size); TP2 = SHORT `zone_bottom − 1.5 ATR`, LONG
  `zone_top + 1.5 ATR` (67%).
- Gates: TP2 reward/risk ≥ 1.5 (partial TP), then **final RR (TP2) ≥ 3.0** (`min_rr_required`).
- Entry: market order; SL/TP1/TP2 are shifted by the fill difference
  (`actual − signal entry`). SL = `stop_market`, TP1/TP2 = `take_profit_market` (reduce-only).

## 6. Trade management

- After TP1 fills: stop of the remaining 67% moves to **entry (break-even)**, and a
  **chandelier trail** activates: each hourly tick, LONG `max(high, last 10 1h bars) − 2.5 ×
  ATR14(1h)`, SHORT mirrored, only tightening, frames incl. the forming bar.
- No time stop in the live code (the README's "50 bars timeout" is not implemented).
- Cooldown: no new entry on a symbol for 48 h after its last close.
- Account-level rules: `max_open_positions = 1` (one position across all 24 instruments),
  `max_total_account_positions = 8` (including another bot), daily loss limit 15% of balance
  (checked **before** signal generation), adaptive size ×0.5 after a loss. Sizing does not
  affect R.

## 7. LONG/SHORT symmetry

Mirrored: zones, in-zone test, confluence, bar-colour/RSI trigger (55/45), filters, SL/TP1/TP2,
trailing. **Not mirrored**: the core LONG target is `top + 6 ATR` vs SHORT `bottom − 3 ATR`
(affects only the coarse 1.5 RR gate). Shorts are enabled (`allow_short_signals = True`);
the log also shows a period of `short_disabled_long_only_mode`.

## 8. Ambiguities and decisions for the port

| # | Ambiguity | Port decision |
|---|---|---|
| A1 | Decisions use forming 1h/4h bars at a variable scan delay | Emulate the scan at HH:00 + **3 min** (median delay); the forming bars are built from the complete 1m bars before HH:03. Fidelity check: each logged scan at its own minute. |
| A2 | RECALC_INTERVAL makes zones/VP/FVG ~48 h stale; depends on frame length | Replicate exactly: 300-bar frame, context from index 251. |
| A3 | 4h context `timestamp ≤ t` includes the 4h bar containing t | Replicate (it is complete at scan time, ~48 h later). |
| A4 | Strategy SL/TP vs engine SL/TP | Engine (live) SL/TP/TP1/TP2 for trades; strategy SL/TP only for the coarse gate. |
| A5 | TP1 (zone mid) can lie on the losing side of the entry | A take-profit trigger already through → fills at the entry minute's next open (immediate). |
| A6 | Account-level rules (1 position across 24 instruments, 8 total incl. another bot, 15% daily loss, adaptive size) | Not portable to a BTC/ETH-only test: dropped. One position **per symbol** at a time; per-symbol 48 h cooldown kept. |
| A7 | Venue: Hyperliquid vs our Binance USD-M data | Port runs on Binance USD-M BTCUSDT/ETHUSDT 1m klines; venue price differences are an expected mismatch source in the fidelity check. |
| A8 | Trailing and TP1 detection run on the bot's tick (hourly + reconciliation), not intrabar | TP1/SL/TP2 resolved on the 1m path (stop first within a minute); the trail is updated at each hourly scan time. |
| A9 | No time stop | None; trades still open at the end of data are closed at the last close and flagged. |
| A10 | Order types | All fills taker (market, stop_market, take_profit_market). |
| A11 | RSI used for the decision vs logged | Decision uses the strategy's SMA RSI; the checker's EWM RSI is logging only. |
| A13 | Reward/risk for the partial-TP and final gates uses `abs()` (a TP2 on the losing side would pass) | Replicated as in the live code; such a TP2 fills immediately (A5). |
| A14 | TP1 is detected and the stop moved to break-even only at the next hourly tick | Replicated: after TP1 the remainder keeps the original stop until the next scan, then BE + trail. |
| A12 | Daily-loss / position / cooldown skips hide whether a signal existed | Those log rows are excluded from the core-signal comparison (regime still compared). |

## 9. Original tests

`test_supply_zone_bot.py` tests the live bot shell: config/credentials, position sizer,
trade logger, position tracker TP1 → break-even, regime detection (rising 140×4h → BULL,
falling → BEAR), conflict/position limits, and the scan path producing TP1/TP2. The port keeps
the strategy-relevant ones (regime, TP1 → break-even + trailing activation, the scan path
returning a signal with TP1/TP2 and the final RR gate) and adds unit tests for zones, VP,
FVGs and filters. Exchange/credential/Telegram/state-file tests have no counterpart.
