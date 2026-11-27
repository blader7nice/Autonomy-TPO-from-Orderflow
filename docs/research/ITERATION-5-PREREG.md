# ITERATION-5 (FINAL, binding hold-out) — TrejdingHub method, revised: pre-registration

Owner instruction of 2026-11-27. Written and committed **before** any ITERATION-5 code, kline
download or run. Referenced from `docs/DECISIONS.md` row `ITERATION-5`; parameters mirrored in
`configs/default.yaml` section `iteration5`.

**Provenance (read first).** The rule changes below were chosen by the owner **after seeing the
ITERATION-4 example charts** (stops hunted at the sweep wick, trades against the current trend)
and add the entry trigger the trader actually uses. The in-sample windows A and B have therefore
informed the design and are not an unbiased test. **The hold-out is the binding verdict.**
ITERATION-4 had been declared the last iteration; the owner explicitly opened ITERATION-5. No
earlier ITERATION-5 instruction or pre-registration exists in this repository, so nothing is
superseded.

## Scope

- Symbols: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT, DOGEUSDT (Binance USD-M 1m klines;
  candles only, no orderflow).
- Windows: **A** = 2024-12-28 … 2025-12-27, **B** = 2026-09-28 … 2026-11-26. Report A, B, A+B.
  Entries only inside A or B. Hold-out **H** = 2025-12-28 … 2026-09-27: no trade is simulated
  or scored there.
- Warm-up: 150 days of klines before each window (daily EMA50 convergence). For A this is
  2024-07-31 … 2024-12-27. For B it is 2026-05-01 … 2026-09-27, i.e. inside H's calendar span:
  **owner decision 2026-11-27** — hold-out candles are used as indicator input only for B
  (EMAs, ATR, prior-day/week levels, zones), as in Phase 3/A/B and ITERATION-4. Window A's data
  ends 2025-12-27 23:59 (trades still open then are closed at the last close, as D-039).
- Costs (all symbols): fee profile `okx_eu_inverse` (taker 0.05 %, maker 0.02 %, UNCONFIRMED
  placeholder, D-033) + **1 bucket** adverse slippage per fill. Sensitivities: maker entry, OKX
  2-tick, OKX 4-tick slippage.
- Execution instruments (OKX EEA public `/api/v5/public/instruments?instType=SWAP`, queried
  2026-11-27): SOL-USD-SWAP (inverse, ctVal 10 USD, tick 0.01), XRP-USD-SWAP (inverse, 10 USD,
  0.0001), DOGE-USD-SWAP (inverse, 10 USD, 0.00001). **BNB has no inverse swap on OKX EEA**;
  the listed BNB-USDT-SWAP (linear, ctVal 0.01 BNB, tick 0.01) is used. BTC/ETH unchanged.
- Binance exchange ticks (`/fapi/v1/exchangeInfo`, 2026-11-27): SOL 0.01, BNB 0.01, XRP 0.0001,
  DOGE 0.00001.
- New-symbol buckets (slippage unit and TPO row size): `bucket = tpo_tick` = 1.5 bps × the
  median 1m close of window A, rounded **up** to a multiple of the Binance tick (BTC's 10 USD
  ≈ 1 bp and ETH's 0.5 USD ≈ 1.7 bp bracket this). Computed from prices only, before any run;
  the resulting values are logged in DECISIONS.

## Common rules

Bars are UTC-aligned aggregates of the 1m klines (15m, 30m, 1h, 4h, 1d); a bar is known at its
close. Entry time `t` = the decision bar's close = the next bar's open; entry price = the open
of the 1m candle at `t`.

1. **Bias at entry** (direction `d`, long +1 / short −1), all three:
   - the last 4H bar closed at or before `t` satisfies ITERATION-4 b1 in direction `d`
     (`close > EMA50` and `EMA50[k] − EMA50[k−3] > 0`; mirrored for shorts);
   - `d × (entry − EMA50_4H[k]) > 0` (entry price on the bias side of that EMA);
   - the last daily bar closed at or before `t`: `d × (close_D − EMA50_D) > 0`.
   EMAs: `ewm(span=50, adjust=False)` of closes, seeded at the first loaded bar. Trades only in
   direction `d`.
2. **POI**: exactly as ITERATION-4 — prior-day TPO VAL/POC (VAH/POC) eligible if below (above)
   the session open, prior-week VAL (VAH) eligible if below (above) the session open and untested
   since the week start, fresh H1/M30 demand (supply) zones (single base candle, FVG > 0, life
   168 h). First test only (one chance per POI, per trigger below).
3. **Stop**: beyond the **far edge** of the POI: `stop = far − d × k × ATR(1h)`. When several
   POIs form one setup, the farthest far edge is used. Never closer than
   `max(0.5 × ATR(1h), 3 × round-trip cost)` from the entry (widened to that distance). A stop
   on the wrong side of the entry makes the setup invalid. ATR(1h) = SMA 14 of the true range of
   complete hours known at `t` (as ITERATION-1/-4). k is a grid dimension {0.25, 0.5}.
4. **Exit**: structural levels = the entry session's prior-day VAH/VAL/POC, the entry week's
   prior-week VAH/VAL/POC and the near edges of opposite zones that exist, are unexpired and
   untested at `t` (the ITERATION-4 set).
   - TP1 = the nearest level strictly beyond the entry. It must be `≥ 1 R` from the entry
     (R = stop distance after the minimum rule); otherwise (or if none) the trade is skipped.
   - 50 % of the position exits at TP1 (fill at the level).
   - The other 50 % (grid dimension):
     - `lvl`: exits at TP2 = the nearest level strictly beyond TP1; the original stop stays.
       If there is no TP2, this half exits only by the stop or the time stop.
     - `trail`: after TP1 fills, a trailing stop at `max(stop, best − 1.5 × ATR(1h))` (long;
       mirrored), `best` = the highest high of the 1m candles from the TP1 minute through the
       previous minute (first active in the minute after TP1; ATR(1h) as at entry). Fill at the
       trail level, or at the minute's open if it opened beyond it.
   - Time stop 48 h after entry (close of the last minute) for whatever is open.
   - Within a minute the stop is checked before any target (if the stop and TP1 are touched in
     the same minute, the whole position stops out).
   - Net R of a trade = the 50/50 average. Costs are linear in fill prices, so the trade is
     costed as one round trip at the average exit price (exact). Exits are costed as taker.
5. **Max 1 trade per symbol per UTC day** (entry day) and no new entry while a trade on that
   symbol is open. Setups are processed in entry-time order; a setup skipped for bias, TP1 < 1 R,
   missing data or an invalid stop does not use the day's slot.

## Entry triggers (grid dimension)

- **T1 "sweep"**: the ITERATION-4 sweep logic on 15m (first test → sweep candidate C beyond the
  far edge by ≥ 1 tick and beyond the latest confirmed 15m 2-bar-fractal swing by ≥ 1 tick →
  close back beyond the far edge on C or C+1 = K → c1 on K: engulfing or close in the outer
  third), **plus** `high[C] − low[C] ≥ 1.5 × ATR(15m)`, ATR(15m) = SMA 14 of the true range of
  the 14 complete 15m bars before C. Decision at K's close; entry at the next 15m open. One setup
  per (direction, K).
- **T2 "zone + confirmation candle"** on 30m bars: the test bar `t0` = the first 30m bar of the
  POI's eligibility whose range reaches the near edge (long: `low ≤ near`). The confirmation
  candle is the first bar `j ∈ {t0, t0+1, t0+2}` (consecutive bars) that closes with
  - (a) a bullish engulfing body (long): `close > open`, `open ≤ min(open, close)[j−1]`,
    `close ≥ max(open, close)[j−1]` and `body[j] > body[j−1]`; or
  - (b) a bullish pin bar (long): lower wick `min(open, close) − low ≥ 2 × |close − open|` and
    `(close − low) / (high − low) ≥ 2/3` (`high > low`);
  - and it does not close beyond the far edge (long: `close ≥ far`).
  Shorts mirrored. If none of the three bars confirms, the POI is consumed without a setup.
  Decision at `j`'s close; entry at the next 30m open. One setup per (direction, j).

## Grid, selection, in-sample gate

Grid: trigger {T1, T2} × k {0.25, 0.5} × second half {lvl, trail} = **8 configurations, no
others**. Cumulative in-sample configurations: 69 + 8 = **77**.

- Selection: highest net expectancy in R (default costs) on A+B, all six symbols combined, among
  configurations with **≥ 100 trades** on A+B; tie → smaller max drawdown.
- **Gate (all required):** selected net expectancy on A+B > 0 R; A and B both with the same
  (positive) sign (a window without trades fails); net expectancy on A+B > 0 R in **≥ 4 of the 6
  symbols** (a symbol without trades counts as not positive). No configuration with ≥ 100
  trades → fail. **Fail → FINAL-KILL stands; STOP.**
- Reported in every case: T1 vs T2 side by side, per-symbol table, cost sensitivities, monthly
  stability, MDE for the hold-out (session-clustered bootstrap SE, 6 symbols), funnel/skips,
  and 10 example charts (3 best, 3 worst, 4 median) of the selected configuration (or of the
  configuration with the most trades if none is eligible), to check the logic visually.
- **Pass → STOP** and wait for the owner's explicit go before the hold-out.

## Hold-out (only after the owner's explicit go; one run, binding)

The selected configuration, frozen (config hash recorded), on H = 2025-12-28 … 2026-09-27,
six symbols, 150-day warm-up before H as indicator input. One run, logged in DECISIONS with the
hold-out run count. Verdict (proposed here so it is fixed before any hold-out data is scored;
the owner may amend it before the go, logged):
- **PASS**: net expectancy at default costs > 0 R and the lower bound of the session-clustered
  bootstrap 90 % CI > 0 R;
- **INCONCLUSIVE**: point estimate > 0 R but the CI includes 0;
- **FAIL**: point estimate ≤ 0 R.

## Ambiguities decided here (conservative where it matters)

1. "Stop beyond the far edge": the stop is placed from the POI's far edge, not from the sweep
   extreme; for T1 the stop can lie inside the already-swept range (literal rule).
2. Merged POIs: the farthest far edge sets the stop (wider stop, smaller R).
3. TP2 for `lvl` missing → second half rides to the stop / time stop (no invented target).
4. No break-even move after TP1 in `lvl` (not in the instruction).
5. The trail uses only completed minutes and fills at the minute open on a gap through it.
6. T2: only the confirmation candle's close is checked against the far edge (literal); earlier
   bars of the 3-bar window may close beyond it.
7. Pin bars need no bullish (bearish) close, only the wick and close-position rules.
8. Engulfing (T2) compares bodies only; the previous candle's colour is not required.
9. ATR(15m) excludes the sweep candle itself (no lookahead into C's range).
