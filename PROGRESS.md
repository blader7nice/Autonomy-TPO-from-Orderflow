# PROGRESS

Current phase: **1 — venue adapter + bootstrap + live collector** (next)

## Phase 0 — Repo skeleton ✅ (2026-11-25)
- Done: uv project (Python 3.12, `uv.lock`), package layout `src/flowlevels/*`, typed settings
  (`flowlevels.settings`, fail-fast) and versioned YAML config with hash
  (`flowlevels.config`), CLI `flowlevels info`, Makefile, CI (ruff, mypy, pytest),
  AGENTS.md, docs/SPEC.md (amended spec), DECISIONS (D-001..D-009), BACKLOG, DATA_SOURCES.
- Verified: `make check` green locally (5 tests).
- Open issues: Binance WS/REST docs unreachable from the dev container → all such facts are
  UNVERIFIED in docs/DATA_SOURCES.md; `make verify-api` on the VPS will check them (D-009).
