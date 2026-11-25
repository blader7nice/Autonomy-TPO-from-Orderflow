# Decisions log

Format: ID — date — decision — reason. Cheap choices are made and logged here.
Changes to thresholds during the forward test MUST be logged here and reset its counter.

| ID | Date | Decision | Reason |
|---|---|---|---|
| D-001 | 2026-11-25 | No network to Binance in the dev container. Phases 0–2 are built against fixtures. Real-data steps run on the VPS through `make` targets. | Owner decision A. |
| D-002 | 2026-11-25 | Add a historical out-of-sample hold-out (the 9 months before the 60-day tuning window). It is the primary GO/ITERATE/KILL decision, uses a paired design (treatment vs unconfirmed touches) and an MDE report, and has a new STOP at Phase 3b. The forward test becomes confirmation with an MDE-based minimum N. | Owner decision B. 100 events cannot resolve a +0.15R uplift (SE ≈ 0.15–0.2R). |
| D-003 | 2026-11-25 | A single git branch with one commit per phase. PRs to main at the end of Phase 2 and Phase 6. | Owner decision C. |
| D-004 | 2026-11-25 | Footprint bucket: BTC 10 USD, ETH 0.5 USD (configurable). | At current prices this is about 1 bp, which gives tens of rows per 5m bar: readable ladders and enough volume per row for imbalance ratios. The exchange tick (0.1 / 0.01) would give sparse, noisy rows. Review if price changes by more than 2×. |
| D-005 | 2026-11-25 | The random control uses the zone-edge stop, the same as baseline and unconfirmed. | Risk 3: the control must share the comparison group's stop geometry. |
| D-006 | 2026-11-25 | Time-of-day baselines use median/MAD (robust z = (x − median) / (1.4826·MAD)) with a weekday/weekend split. If a slot has fewer than `baseline_min_samples` samples, it is treated as having no baseline and its features are None. | Risk 4: with 20 samples per slot, weekends mixed into weekday history distort the mean and SD. |
| D-007 | 2026-11-25 | The parity test snapshots the bootstrapped state (levels, baselines, CVD) and starts both the live and the replay run from that same snapshot. | Risk 5: parity cannot hold if the initial states differ. |
| D-008 | 2026-11-25 | Cost geometry: report costs in R, enforce a minimum stop distance (config), run a sensitivity with treatment on the zone-edge stop, and report every net result in R and in bps. | Risk 2: tight stops inflate costs measured in R. |
| D-009 | 2026-11-25 | Binance WS/REST details could not be verified from official docs: developers.binance.com is unreachable from the dev container. All of them are marked UNVERIFIED in DATA_SOURCES.md, all URLs and stream names are configurable, and `make verify-api` checks them on the VPS before any real run. Fixtures built from these assumptions are labelled synthetic. | Hard rule: never invent API behavior. Verification is deferred, not skipped. |
