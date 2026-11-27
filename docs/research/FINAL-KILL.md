# FINAL KILL — no candidate for the hold-out (MASTER-PLAN Phase D, confirmed by ITERATION-4), 2026-11-27

**Update (ITERATION-4): KILLED — no configuration reached 60 trades on A+B.**

**Update (ITERATION-5, FINAL, binding hold-out, opened by the owner): KILLED at the in-sample
gate — all 8 configurations negative on A+B. FINAL-KILL stands; the hold-out was not run.
Cumulative configurations: 77.**

Phase D rule (fixed before any run): eligible = a surviving rule or combination with net
expectancy > 0 R on A+B, the same sign in A and B, and ≥ 60 trades. **No rule survived
Phase A or B, so Phases C/C2 did not apply and no candidate is eligible.** The 9-month
hold-out (2025-12-28 … 2026-09-27) was never loaded or run (0 runs).

Costs everywhere: OKX EEA inverse perps, taker 0.05 % (UNCONFIRMED placeholder) + 1 footprint
bucket slippage per fill, with maker-entry and OKX 2/4-tick sensitivities.

## All iterations

| iteration | idea | window(s) | configs | best / selected result (net, default costs) | verdict |
|---|---|---|---|---|---|
| Phase 3 | orderflow confirmation (5 detectors) at static zone touches, fixed-R exits | 60 d tuning (= B) | 36 | 5m/W2/thr3: −0.40 R (N 41); uplift vs unconfirmed +0.35 R, 90 % CI [−0.007, +0.711]; negative in all 36 | not powered (MDE 0.25 R); owner: no hold-out |
| ITERATION-1 | confirmation as entry timing, level-to-level, stop ≥ 3 × RT cost | B | 12 | max 10 trades per config; all 12 negative (−0.36 … −1.33 R) | KILLED |
| Phase A (ITERATION-2) | owner's Supply Zone bot, faithful port | A + B | 1 | 2 trades in 425 days, A+B −1.157 R | KILLED |
| Phase B H1 | Dalton 80 % rule | A + B | 4 | k0.5/none: N 246, A −0.033, B −0.506, A+B −0.085 R | KILLED |
| Phase B H2 | Dalton balance fade | A + B | 8 | k0.5/none/r1.5: N 300, A −0.045, B −0.368, A+B −0.098 R | KILLED |
| ITERATION-4 (FINAL) | TrejdingHub method replica: 4H bias → fresh POI (prior-day/week TPO, H1/M30 zones) → 15m liquidity sweep → candle (c1) / + M30 CVD (c2) confirmation → structural target ≥ minRR | A + B | 8 | max N 40 (b1/c1/rr2): A −0.265, B +0.173, A+B −0.232 R; 7 of 8 negative | KILLED (no config ≥ 60 trades) |
| ITERATION-5 (FINAL) | revised method: 4H + daily EMA50 bias at entry, ITERATION-4 POIs, T1 sweep (range ≥ 1.5 ATR15) or T2 30m zone + engulfing/pin, stop at POI far edge + k ATR, 50 % TP1 (≥ 1 R) + 50 % TP2 or ATR trail, 48 h; 6 symbols | A + B | 8 | T2/k0.5/trail: N 218, A −0.116, B +0.127, A+B −0.079 R, 2/6 symbols > 0; all 8 negative | KILLED (gate) |

**Cumulative configurations tried in the in-sample windows: 77** (36 + 12 + 1 + 12 + 8 + 8). The
superseded ITERATION-2 confirmation grid (6) was never run and is not counted.

## What was learned

1. **Costs decide intraday level trading on these instruments.** A round trip costs ~13 bps
   (2 × 0.05 % + 2 × 1 bucket). Phase 3 stops at zone touches had a median of ~15 bps, so costs
   took ~0.6–1.3 R per trade. Gross expectancy of random entries was ≈ 0, which says the
   accounting is sound and the edge is missing, not hidden.
2. **Orderflow confirmation carried weak information, not a tradable edge.** Phase 3 showed the
   ordering random < unconfirmed < confirmed. The uplift CI included 0 and the confirmed group
   stayed negative in all 36 configs. Widening stops to escape costs (ITERATION-1) left almost
   no setups with a reachable next level.
3. **Dalton rules are near break-even in A and clearly negative in B.** With maker entry, A
   turns slightly positive (H1 +0.03, H2 +0.05 R) but A+B stays ≤ 0. Any edge is smaller than
   the costs and not stable across regimes.
4. **The Supply Zone bot is too sparse on BTC/ETH to evaluate.** The port matches the original
   code exactly and the live log reasonably well (regime 97 %, positives 66 %). The final RR
   ≥ 3 gate and the filters leave 2 trades in 14 months on BTC+ETH, consistent with 0 live
   BTC/ETH entries. Its live trades came from altcoins, which were outside this plan.
5. **Fees are still a placeholder.** All verdicts use taker 0.05 %. The maker-entry rows show
   that the conclusion is sensitive to execution cost but does not flip on A+B for any rule.
   A re-test with confirmed lower fees would be a new, pre-registered iteration.

6. **The full sequential method is too selective and still negative.** 4H bias, a first test of
   a fresh POI, a sweep of the 15m swing and a close back produce ~2,550 setups on 425 days
   (BTC+ETH), but the bias and the "nearest structural target ≥ 2R" rule leave ≤ 40 trades per
   configuration. Those trades lose (27 % wins at a 2.6 R median target, −0.23 R net); costs are
   ~0.27 R per trade. The M30 orderflow filter (c2) cuts N to 1–9 and cannot be judged.
   Report: `docs/research/ITER4-20261127T182244Z-iter4-0610dfbc.md`; charts: `docs/research/TRADES-ITER4.md`.

7. **Wider, structure-anchored stops and a trend filter did not create an edge.** ITERATION-5
   moved the stop behind the POI's far edge (median 114 bps, 1.18 × ATR(1h); costs fell to
   0.14 R per trade), required the 4H and daily EMA50 on the trade's side, split the exit, and
   added the trader's own trigger (30m zone touch + engulfing / pin bar). On six symbols this
   gives 144–330 trades per configuration, all net negative on A+B (−0.08 … −0.42 R). The candle
   trigger (T2) beats the sweep trigger (T1) by 0.12–0.25 R but is itself ≤ 0; only SOL and XRP
   are slightly positive. Report: `docs/research/ITER5-20261127T194620Z-iter5-da431f34.md`; charts:
   `docs/research/TRADES-ITER5.md`.

## What remains usable

Data pipeline (aggTrades → 1m footprint with hole repair, kline store), level engine and
zones, five orderflow detectors, the research harness (one code path, no-lookahead tests,
session bootstrap, MDE), the exact Supply Zone port with its fidelity tools, and the Dalton
TPO rules. Reports: `docs/research/`.
