# Level statistics BEFORE: config v1 (height-based HVN/LVN, single-linkage confluence)

Generated 2026-11-26 16:23 UTC, commit `6f07536fd58c`, config v1 hash `d64cccb9c91db67c`.

Data holes (not repaired, D-026/D-031): BTCUSDT 2026-10-27 00:02:46–2026-10-27 00:30:05; ETHUSDT 2026-10-27 00:02:47–2026-10-27 00:30:05 (54.6 min in total). Excluded sessions (level inputs overlap a hole): 24 of 58 (41.4%): BTCUSDT 2026-10-28, BTCUSDT 2026-10-29, BTCUSDT 2026-10-30, BTCUSDT 2026-10-31, BTCUSDT 2026-11-01, BTCUSDT 2026-11-02, BTCUSDT 2026-11-03, BTCUSDT 2026-11-04, BTCUSDT 2026-11-05, BTCUSDT 2026-11-06, BTCUSDT 2026-11-07, BTCUSDT 2026-11-08, ETHUSDT 2026-10-28, ETHUSDT 2026-10-29, ETHUSDT 2026-10-30, ETHUSDT 2026-10-31, ETHUSDT 2026-11-01, ETHUSDT 2026-11-02, ETHUSDT 2026-11-03, ETHUSDT 2026-11-04, ETHUSDT 2026-11-05, ETHUSDT 2026-11-06, ETHUSDT 2026-11-07, ETHUSDT 2026-11-08.

Definitions: `flowlevels.levels.stats` docstring. Confluence zone = zone with >= 2 members; widths are high − low incl. tolerance, over all 24 hourly snapshots.

| symbol | sessions | levels/day | HVN+LVN/day | zones/snapshot | conf zones/snapshot | conf zones in session range | width mean USD | width max USD | width mean ATR | width max ATR |
|---|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 17 | 107.82 | 82.65 | 15.08 | 4.50 | 2.18 | 859.72 | 5910.70 | 1.41 | 8.69 |
| ETHUSDT | 17 | 87.35 | 62.88 | 16.45 | 5.35 | 2.65 | 24.49 | 197.38 | 1.18 | 7.24 |

## Per session

| symbol | day | levels | HVN+LVN | zones/snap | conf/snap | conf in range | max width USD | max width ATR |
|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 2026-11-09 | 90 | 71 | 7.4 | 3.2 | 1.1 | 2850.4 | 4.86 |
| BTCUSDT | 2026-11-10 | 86 | 61 | 9.1 | 4.0 | 2.2 | 2243.1 | 5.30 |
| BTCUSDT | 2026-11-11 | 106 | 81 | 10.1 | 5.2 | 2.0 | 2808.6 | 5.96 |
| BTCUSDT | 2026-11-12 | 60 | 35 | 14.4 | 5.3 | 1.9 | 1590.5 | 3.83 |
| BTCUSDT | 2026-11-13 | 112 | 87 | 15.8 | 4.0 | 2.0 | 3539.5 | 6.35 |
| BTCUSDT | 2026-11-14 | 117 | 91 | 19.8 | 5.8 | 2.8 | 3531.2 | 6.01 |
| BTCUSDT | 2026-11-15 | 190 | 163 | 16.6 | 3.8 | 1.3 | 5910.7 | 8.00 |
| BTCUSDT | 2026-11-16 | 145 | 123 | 13.7 | 4.4 | 1.8 | 4325.0 | 7.16 |
| BTCUSDT | 2026-11-17 | 80 | 53 | 15.7 | 2.7 | 1.4 | 1843.2 | 3.93 |
| BTCUSDT | 2026-11-18 | 66 | 39 | 16.8 | 4.2 | 1.9 | 2090.0 | 4.24 |
| BTCUSDT | 2026-11-19 | 105 | 79 | 14.1 | 3.3 | 2.3 | 3225.0 | 6.32 |
| BTCUSDT | 2026-11-20 | 102 | 75 | 14.3 | 3.5 | 1.3 | 3207.1 | 4.94 |
| BTCUSDT | 2026-11-21 | 145 | 117 | 16.7 | 7.1 | 1.8 | 4874.1 | 5.01 |
| BTCUSDT | 2026-11-22 | 170 | 141 | 23.4 | 7.0 | 4.2 | 5703.3 | 8.69 |
| BTCUSDT | 2026-11-23 | 80 | 57 | 15.4 | 4.2 | 3.8 | 3167.7 | 4.28 |
| BTCUSDT | 2026-11-24 | 109 | 85 | 16.3 | 5.8 | 3.3 | 3713.7 | 5.30 |
| BTCUSDT | 2026-11-25 | 70 | 47 | 16.8 | 2.8 | 1.8 | 1865.5 | 3.18 |
| ETHUSDT | 2026-11-09 | 68 | 49 | 13.4 | 6.8 | 3.1 | 80.7 | 2.38 |
| ETHUSDT | 2026-11-10 | 76 | 51 | 16.0 | 4.2 | 2.8 | 80.0 | 3.88 |
| ETHUSDT | 2026-11-11 | 96 | 71 | 14.7 | 6.9 | 1.9 | 104.6 | 4.77 |
| ETHUSDT | 2026-11-12 | 51 | 25 | 17.5 | 6.0 | 1.8 | 50.4 | 2.91 |
| ETHUSDT | 2026-11-13 | 63 | 37 | 18.9 | 5.9 | 2.1 | 60.5 | 3.03 |
| ETHUSDT | 2026-11-14 | 101 | 75 | 19.8 | 3.3 | 2.1 | 134.0 | 5.84 |
| ETHUSDT | 2026-11-15 | 147 | 121 | 19.0 | 5.8 | 3.5 | 149.4 | 4.18 |
| ETHUSDT | 2026-11-16 | 92 | 71 | 13.2 | 2.7 | 1.4 | 135.1 | 6.59 |
| ETHUSDT | 2026-11-17 | 71 | 45 | 15.1 | 3.9 | 2.0 | 57.7 | 3.25 |
| ETHUSDT | 2026-11-18 | 93 | 67 | 12.5 | 3.7 | 1.9 | 107.3 | 5.74 |
| ETHUSDT | 2026-11-19 | 78 | 53 | 14.6 | 5.0 | 3.1 | 89.8 | 4.24 |
| ETHUSDT | 2026-11-20 | 74 | 49 | 14.7 | 3.6 | 2.4 | 97.5 | 4.05 |
| ETHUSDT | 2026-11-21 | 138 | 113 | 13.9 | 4.1 | 1.7 | 197.4 | 6.77 |
| ETHUSDT | 2026-11-22 | 103 | 77 | 29.0 | 13.5 | 5.8 | 49.2 | 2.71 |
| ETHUSDT | 2026-11-23 | 70 | 49 | 15.1 | 6.8 | 4.5 | 94.1 | 3.49 |
| ETHUSDT | 2026-11-24 | 111 | 87 | 14.9 | 4.2 | 2.1 | 162.6 | 7.24 |
| ETHUSDT | 2026-11-25 | 53 | 29 | 17.3 | 4.5 | 2.8 | 59.7 | 3.12 |
