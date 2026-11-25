# AGENTS.md — read first, every session

Every session starts by reading this file, `PROGRESS.md` and `docs/SPEC.md`.
`docs/SPEC.md` is the authoritative design (incl. the 2026-11-25 amendments).

## Hard rules (non-negotiable)

1. **One code path** for history and live: everything consumes normalized trade events
   through `TradeSource`. Historical = replay, live = WebSocket. No separate backtest logic.
2. **No lookahead.** Anything at time t uses only data with `ts < t` (or `<=` bar close for
   bar-close features, stated explicitly). Enforced by property tests.
3. **Exchange timestamps only** (trade time `T`), never the local clock, in any logic.
4. **Aggressor side**: Binance aggTrade `m = true` => buyer is maker => aggressor is the
   SELLER (`'S'`). Unit-tested.
5. **Thresholds are config** (`configs/*.yaml`), never code. Every result records the config
   hash and git commit.
6. **Costs always on.** All results net of fees + slippage. Report in R *and* in bps.
7. **Settings/secrets**: pydantic-settings, fail fast. `.env` is never committed.
8. **Reproducible**: `uv.lock`, fixed seeds, `PYTHONDONTWRITEBYTECODE=1` in services, run
   from the installed package (`flowlevels` CLI), not loose scripts.

## Research integrity

- The **hold-out** (9 months before the 60-day tuning window) is run once with frozen
  thresholds and is never used to choose parameters. Any re-run is logged in
  `docs/DECISIONS.md` and the hold-out run count is reported.
- Primary comparison is **paired**: confirmed touches vs unconfirmed touches of the same zone
  types, plus a random control. Report MDE/power before the hold-out run.
- A positive edge in random entries is a bug signal; investigate before trusting results.
- A KILL verdict is a valid, useful result. Do not tune until it looks good.

## Working rules

- Work phase by phase (`docs/SPEC.md` §10). End of phase: all tests green, commit, update
  `PROGRESS.md`. Stop at STOP points.
- Never invent exchange API behavior. Anything not verified against official docs is marked
  UNVERIFIED in `docs/DATA_SOURCES.md` and must be checked by `make verify-api` on the VPS.
- No network to Binance in the dev container: build against fixtures; real data runs on the
  VPS via documented `make` targets (README "Running on the VPS"). Never fake data.
- Cheap decisions: decide and log in `docs/DECISIONS.md`. Expensive/irreversible: ask.
- Ideas outside the spec go to `docs/BACKLOG.md`.
- Do not touch other repositories. Bot integration = client library + docs only.
- Git: single branch `claude/flowlevels-orderflow-confirmation-ups9bz`, one commit per
  phase; PR to main at end of Phase 2 and Phase 6.

## Commands

`make check` (ruff + mypy + pytest) must pass before every commit.
