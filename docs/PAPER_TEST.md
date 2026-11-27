# Paper forward test — ITERATION-5 method (fixed 2026-11-27)

The research verdict is **FINAL-KILL** (`docs/research/FINAL-KILL.md`): no ITERATION-5
configuration was positive in sample. The paper test observes the method forward in time with
**no real orders and no exchange keys**. It cannot overturn the research verdict by itself; it can
only produce evidence for the owner's decision under the rules below. Plan and design choices:
`docs/DECISIONS.md` row `PAPER-PLAN`.

## Variants (each with its own ledger)

| id | key | trigger | stop | second half |
|---|---|---|---|---|
| a | `T2\|k0.5\|trail` | 30m zone touch + engulfing / pin | POI far edge − 0.5 × ATR(1h) | 1.5 × ATR(1h) trail after TP1 |
| b | `T2\|k0.5\|lvl` | 30m zone touch + engulfing / pin | POI far edge − 0.5 × ATR(1h) | next structural level ("next") |
| c | `T2\|wick\|trail` | 30m zone touch + engulfing / pin | confirmation-candle wick − 0.1 × ATR(1h) | 1.5 × ATR(1h) trail after TP1 |
| d | `T1\|k0.5\|lvl` | 15m sweep + close-back + candle | POI far edge − 0.5 × ATR(1h) | next structural level ("next") |

Common: 4H + daily EMA50 bias at entry, POIs = prior-day/week TPO levels + fresh H1/M30 zones
(first test), stop never closer than max(0.5 × ATR(1h), 3 × round-trip cost), 50 % at TP1
(nearest structural level, must be ≥ 1 R or the trade is skipped), 48 h time stop, max 1 trade
per symbol per UTC day per variant. Fills at the next-bar open; OKX default costs (taker
0.05 %, placeholder) + 1 bucket slippage per fill, as in research.

**Versions.** Rules are frozen per version (a hash of every rule-relevant input). Any rule
change creates a new version with its own ledger and its own trade counter starting at 0.

## Decision rules (fixed now, before any paper trade)

1. **No rule changes before a variant has ≥ 30 closed trades** in its current version. Weekly
   reviews are for bugs and visual checks only. A bug fix that changes trade logic is a rule
   change (new version, counter reset) and is logged in `docs/DECISIONS.md`.
2. **Real-money trading is only considered** for a variant (version) with **≥ 50 closed trades**,
   **positive net expectancy** at default costs **and** a **90 % confidence-interval lower bound
   above −0.1 R**, and **only by the owner's explicit decision**. Nothing is ever traded
   automatically.
3. The CI is computed as in research: mean net R per trade with a session-clustered bootstrap
   (sessions = symbol × UTC entry day; 10,000 resamples, seed 42), 90 % two-sided interval
   `mean ± 1.645 × SE`. The dashboard shows it from 10 closed trades on; it is only binding at
   ≥ 50.
4. Trades detected late (after engine downtime, from backfilled data) count, and their number is
   reported. Trades are never removed from a ledger.
5. Comparing variants is descriptive. With four variants, the best one is expected to look
   better than it is (selection); the owner decides with that in mind.

## How to read the dashboard

The dashboard is read-only: it shows the engine's SQLite ledger and never changes it. All
times are UTC. All R and bps values are **net of costs** (OKX default fees + 1-bucket slippage
per fill). A yellow **DEMO DATA** banner means the page shows the demo database (window B
replayed through the engine for screenshots), not the live ledger.

**Status bar (top, every page).** Engine dot: green = running, amber = demo, red = stalled or
stopped (no heartbeat for 3 min; Telegram alerts at the same time). *Data lag* = age of the
newest closed 1m candle (normal: 4–70 s; alert after 5 min). *Updated Xs ago* = last engine
write. *Open* = open paper positions over all variants. A "Reconnecting…" chip means the
browser lost the live connection; the page falls back to polling every 30 s.

**Chart.** Symbol chips and 15m / 30m / 1h / 4h. Candles of closed bars only. Solid white step
line = 4H EMA50, dashed = daily EMA50 (step lines: the value that was known at each close; the
daily EMA is often off-screen far from price, its value is in the side panel). The badge (LONG /
SHORT / NO BIAS) and the "Bias at the last close" card show the three bias conditions; trades
only go with the bias. Green / red translucent bands = fresh demand / supply zones (H1, M30)
before their first test; amber = being tested now. Dashed grey lines with axis labels = prior
day / week TPO levels (pdVAH, pdPOC, pdVAL, pwVAH, pwPOC, pwVAL; levels that sit close
together share one label). Markers: arrow with a letter = an entry of that variant; dot = exit
(green win, red loss); small grey dot = a setup that was seen but skipped (bias, day limit,
TP1 < 1 R, …). Open positions are drawn as lines: white entry, red stop, green TP1 (and TP2
for b / d). The readout at the top left shows the OHLC under the crosshair.

**Positions.** One column per variant. The big number is the R **if the position were closed
now** (net of costs, including the half already closed at TP1). The rail shows stop (−1 R),
entry (0) and targets, and a dot at the current price.

**Trades.** All trades, newest first; filter by variant, symbol and result. *Exit* tells how
the trade ended (stop, TP1 + trail, TP1 + TP2, time stop, …). LATE = detected after engine
downtime from backfilled data (counts like any other trade). Tap a trade for its chart with
every rule element (POI band, amber squares = touch and confirmation candle, entry arrow,
exits x1 / x2, stop and targets) and the **"Why it was taken"** list: each rule with the
values it saw at the decision time. The PNG link is the same chart as sent to Telegram.

**Performance.** One tile per variant: cumulative net R, closed trades (+ open), win rate,
expectancy per trade, max drawdown, 90 % CI (from 10 closed trades on), and the progress bar
toward 30 (no rule changes before it: FROZEN) and 50 closed trades (decision rule). The status
badge reads FROZEN n/30, REVIEW OK (30–49), RULE MET or RULE NOT MET (≥ 50). The CI only
turns green / red once it is binding (≥ 50). *Paper vs backtest* compares each variant with its
A+B in-sample backtest (`configs/default.yaml` `paper.backtest`); remember all four were
negative in the backtest. The weekly table groups closed trades by ISO entry week.

**Health.** Engine heartbeat, uptime, commit and config hash, Telegram on / off; disk use
(alert at 70 %); notification queue (sent / pending / failed); per-symbol feed lag and the time
one full recomputation took; the rule version of every ledger; recent events (warnings,
divergence alerts, restarts).
