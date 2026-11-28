# Paper-test reference backtest — run `20261127T211931Z-paperref-a086f3e1`

Commit `2bf76dce89f7`, config `a086f3e1325b2119`. Plan: DECISIONS `PAPER-PLAN`. Windows A 2024-12-28 … 2025-12-27, B 2026-09-28 … 2026-11-26; hold-out 2025-12-28 … 2026-09-27 not scored (0 runs; B's warm-up uses hold-out-span candles as indicator input only). Costs `okx_eu_inverse` + `bucket1`. Run time 25 s, peak RSS 0.36 GB. This is a reference run for the paper test; it changes no verdict (FINAL-KILL stands).

## Paper variants on A and B (default costs, six symbols)

| variant | key | N A | exp A | N B | exp B | N A+B | exp A+B R | bps | win | PF | max DD R | symbols > 0 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| a | `T2|k0.5|trail` | 185 | -0.116 | 33 | +0.127 | 218 | -0.079 | -7.7 | 39.9% | 0.88 | 23.8 | 2/6 |
| b | `T2|k0.5|lvl` | 184 | -0.204 | 32 | +0.436 | 216 | -0.109 | -7.3 | 38.4% | 0.84 | 40.0 | 2/6 |
| c | `T2|wick|trail` | 545 | -0.268 | 86 | +0.079 | 631 | -0.221 | -14.0 | 34.7% | 0.70 | 157.3 | 1/6 |
| d | `T1|k0.5|lvl` | 127 | -0.238 | 17 | -0.171 | 144 | -0.230 | -12.6 | 35.4% | 0.68 | 34.1 | 1/6 |

Consistency (recomputed ITERATION-5 configurations vs the stored run):

- `T1|k0.5|lvl`: 144 trades vs ITERATION-5 144 → **identical**
- `T2|k0.5|lvl`: 216 trades vs ITERATION-5 216 → **identical**
- `T2|k0.5|trail`: 218 trades vs ITERATION-5 218 → **identical**

### New variant `T2|wick|trail` (reference run; no verdict changes)

| group | N | win | exp R | exp bps | PF | MFE R | MAE R | max DD R | cost R | R p5 / p50 / p95 |
|---|---|---|---|---|---|---|---|---|---|---|
| A | 545 | 33.0% | -0.268 | -16.4 | 0.65 | 1.81 | 0.82 | 152.9 | 0.213 | -1.33 / -1.14 / +2.46 |
| B | 86 | 45.3% | +0.079 | +0.6 | 1.12 | 2.39 | 0.77 | 11.2 | 0.280 | -1.33 / -0.67 / +2.89 |
| A+B | 631 | 34.7% | -0.221 | -14.0 | 0.70 | 1.89 | 0.81 | 157.3 | 0.222 | -1.33 / -1.14 / +2.60 |

Cost sensitivity (expectancy R):

| scenario | A | B | A+B | A+B bps |
|---|---|---|---|---|
| default | -0.268 | +0.079 | -0.221 | -14.0 |
| maker_entry | -0.194 | +0.183 | -0.143 | -9.3 |
| slip_okx_2tick | -0.236 | +0.137 | -0.185 | -12.0 |
| slip_okx_4tick | -0.255 | +0.094 | -0.208 | -13.5 |

Per symbol (A+B; A / B in the last column):

| group | N | win | exp R | exp bps | PF | MFE R | MAE R | max DD R | cost R | R p5 / p50 / p95 | A / B |
|---|---|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 70 | 28.6% | -0.642 | -31.2 | 0.25 | 1.36 | 0.85 | 46.5 | 0.271 | -1.33 / -1.22 / +1.08 | -0.641 / -0.648 |
| ETHUSDT | 110 | 40.0% | +0.008 | -7.6 | 1.01 | 2.22 | 0.77 | 23.2 | 0.230 | -1.33 / -1.11 / +2.70 | -0.170 / +0.814 |
| SOLUSDT | 116 | 36.2% | -0.165 | -12.8 | 0.77 | 1.97 | 0.80 | 22.3 | 0.205 | -1.33 / -1.12 / +2.46 | -0.187 / -0.033 |
| BNBUSDT | 92 | 30.4% | -0.334 | -20.8 | 0.58 | 1.73 | 0.87 | 34.6 | 0.254 | -1.33 / -1.20 / +2.48 | -0.371 / +0.003 |
| XRPUSDT | 115 | 35.7% | -0.126 | -5.7 | 0.82 | 1.96 | 0.79 | 29.5 | 0.214 | -1.33 / -1.10 / +2.68 | -0.144 / -0.008 |
| DOGEUSDT | 128 | 34.4% | -0.242 | -14.0 | 0.68 | 1.90 | 0.81 | 38.6 | 0.188 | -1.33 / -1.12 / +3.06 | -0.249 / -0.187 |

Exits {'stop': 381, 'tp1+time': 1, 'tp1+trail': 249}; median stop 61 bps (0.59 × ATR(1h)) vs `T2|k0.5|trail` 114 bps; median TP1 1.43 R.

Monthly: 25% of months with net R > 0. Session-clustered SE 0.060 R → 90 % CI [-0.319, -0.122] R.

| month | N | sum R | mean R |
|---|---|---|---|
| 2024-12 | 4 | -2.47 | -0.616 |
| 2025-01 | 44 | -5.70 | -0.130 |
| 2025-02 | 58 | -2.75 | -0.047 |
| 2025-03 | 57 | -3.94 | -0.069 |
| 2025-04 | 33 | -8.08 | -0.245 |
| 2025-05 | 43 | -7.00 | -0.163 |
| 2025-06 | 44 | -23.01 | -0.523 |
| 2025-07 | 49 | -18.42 | -0.376 |
| 2025-08 | 35 | +3.72 | +0.106 |
| 2025-09 | 43 | -9.81 | -0.228 |
| 2025-10 | 42 | -25.13 | -0.598 |
| 2025-11 | 50 | -18.15 | -0.363 |
| 2025-12 | 43 | -25.28 | -0.588 |
| 2026-09 | 3 | +0.88 | +0.292 |
| 2026-10 | 31 | +1.28 | +0.041 |
| 2026-11 | 52 | +4.62 | +0.089 |

Skips (A+B, all symbols): bias 5587, daily_limit 482, no_target 236, outside_window 5643, position_open 42, tp1_below_1r 1776

**Cumulative configurations tried in the in-sample windows: 77 + 1 = 78.** Hold-out runs: 0.
