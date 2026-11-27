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
