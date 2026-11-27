# Supply Zone port — fidelity check (MASTER-PLAN Phase A.2), 2026-11-27

Candles only: Binance USD-M 1m klines 2026-04-01 … 2026-06-06 for all 34 base assets in the
live log (no orderflow, no outcomes; the log period lies in the hold-out span). Each logged
scan was replayed at its logged time (forming bars from the 1m candles before that time).
The raw log is not in the repository. Commands: `flowlevels sz-fidelity`, `flowlevels sz-refcheck`.

## 1. Port vs the live log (pre-registered thresholds)

| metric | value | threshold | |
|---|---|---|---|
| symbols checked (Binance data for all) | 34 / 34 | | |
| regime agreement (15,781 scans) | **97.4 %** | ≥ 90 % | pass |
| core-signal agreement (10,622 comparable scans) | **98.96 %** | ≥ 90 % | pass |
| logged core positives reproduced | **131 / 199 = 65.8 %** | ≥ 60 % | pass |
| port positives on logged negatives | 43 / 10,423 (0.4 %) | | |

**Verdict: fidelity adequate → Phase A proceeds to the strategy test.**

By log period:

| period / venue in the log | regime agreement | positives reproduced |
|---|---|---|
| `*/USDC:USDC` (Hyperliquid, current code) | 99.8 % (9,984) | 102 / 141 = 72 % |
| `*/USDT:USDT` (earlier bot version, Binance **testnet**) | 93.2 % (5,797) | 29 / 58 = 50 % |

## 2. Port vs the ORIGINAL code on identical candles

The owner's pandas code (`supply_zone_short.py`, `signal_checker.py`, `config.py`, and the
engine/executor regime/SL/TP methods extracted verbatim) was run outside the repository on
the exact frames the port used: 3,958 scans (every scan where the log or the port reached the
core signal, plus 1,500 random others).

| metric | value |
|---|---|
| regime agreement | **100 %** |
| decision reason agreement | **100 %** |
| SL / TP1 / TP2 mismatches on signals | **0** |

The port's logic is exact, so every disagreement with the log comes from the inputs.

## 3. Mismatches explained

- **Venue prices**: the bot traded on Hyperliquid (and, earlier, Binance testnet); the port
  uses Binance USD-M mainnet. On reproduced positives the decision price differs by median
  5.5 bps (p90 23 bps). Zones use a 0.5 % tolerance and the core signal needs the price inside
  a zone with the right bar colour/RSI, so a few bps flip borderline scans: 63 of the 68
  missed positives have the correct regime and fail only the core signal.
- **Volume**: the VP POC (confluence) and the volume-spike filter use venue volume.
  Confluence equals the log in 70 % of reproduced positives, and the filter verdict in 86 %.
- **Older bot version in the USDT period**: lower regime agreement (93 %) on testnet candles;
  testing the code-default threshold 0.1 for those rows made it worse (82.6 %), so the
  per-base thresholds were kept.
- **Scan timing**: logged scans at HH:00:34–HH:06; the forming bar uses complete 1m candles
  before the logged time (seconds inside the current minute are not representable).
