# FINAL KILL — no candidate for the hold-out (MASTER-PLAN Phase D), 2026-11-27

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

**Cumulative configurations tried in the in-sample windows: 61** (36 + 12 + 1 + 12). The
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

## What remains usable

Data pipeline (aggTrades → 1m footprint with hole repair, kline store), level engine and
zones, five orderflow detectors, the research harness (one code path, no-lookahead tests,
session bootstrap, MDE), the exact Supply Zone port with its fidelity tools, and the Dalton
TPO rules. Reports: `docs/research/`.
