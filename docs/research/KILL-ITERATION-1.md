# KILL — orderflow-confirmation trading thesis (ITERATION-1)

Date 2026-11-27. Run `20261127T123132Z-it1-17320d07` (commit fda6ff5, config 17320d07), tuning window
2026-09-28 … 2026-11-26, BTCUSDT + ETHUSDT, 120/120 sessions. Pre-registered in DECISIONS
`ITERATION-1` (commit 4e1f1f0) before any code or run. **Hold-out: not run (0 runs).**

**Verdict: KILLED.** None of the 12 pre-registered configurations reached the required
100 treatment trades. The most any configuration produced was 10 (threshold 2, W 3, k 0.5), so
no configuration can be selected. The pre-registered interpretation counts that as KILL.
Treatment net expectancy was also negative in **all 12** configurations
(−0.36 R … −1.33 R; the best was −0.36 R on N = 5). Full table:
`REPORT-20261127T123132Z-it1-17320d07.md`.

Why:
- **Costs vs geometry.** With round-trip costs of ~13 bps, a minimum stop of
  3 × round-trip cost (~40 bps) binds in ~90% of trades even at k = 0.5 × ATR. A ≥ 1.5 R
  target then needs the next static zone ≥ ~64 bps away. Adjacent static zones are usually
  closer (~14 zones per session), so ~85% of first-touch setups in every group are skipped
  as `target_too_close`.
- **Confirmation is rare.** Of 701 first-touch setups, ≈ 94 confirm at threshold 2 within
  3 bars before the target filter, and 1–2 at threshold 3.
- **No hidden edge in the survivors.** Unconfirmed first touches are −0.48 … +0.20 R
  (N 37–109), baseline −0.41 … −0.52 R, random −0.19 … −0.49 R net. Random gross ≈ 0, so the
  control behaves and there is no bug signal.

Research-integrity record: tuning-window configurations tried in total **48** (36 Phase 3 +
12 ITERATION-1). The zone-score filter was inactive (median = minimum score = 1, D-037).
Peak RSS 0.44 GB, 24 s. As pre-registered, the chunked processing and the 9-month download
(steps that only applied after a positive result) were **not** done.

What remains valid and reusable: the data pipeline (88 days, hole repair, kline-validated
footprint), the level engine and zones, the detectors, and the research harness. A new
thesis needs a new pre-registration and owner approval.
