SHELL := /bin/bash
export PYTHONDONTWRITEBYTECODE := 1
RUN := uv run --frozen

DAYS ?= 30
SYMBOLS ?= BTCUSDT ETHUSDT
DURATION ?= 3600
TS ?=
SYMBOL ?= BTCUSDT
RES ?= 5m
TODAY := $(shell date -u +%Y-%m-%d)

.PHONY: help sync lint format typecheck test check verify-api bootstrap validate-klines \
        live-smoke levels footprint sanity-plots levels-stats backtest

help:
	@grep -E '^[a-z-]+:' Makefile | cut -d: -f1

sync:
	uv sync --frozen

lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .

format:
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

typecheck:
	$(RUN) mypy

test:
	$(RUN) pytest

check: lint typecheck test

# ---- Real-data steps (VPS only; see README "Running on the VPS") ----
verify-api:
	$(RUN) flowlevels verify-api --symbols $(SYMBOLS)

bootstrap:
	$(RUN) flowlevels bootstrap --days $(DAYS) --symbols $(SYMBOLS)

validate-klines:
	$(RUN) flowlevels validate-klines --days $(DAYS) --symbols $(SYMBOLS)

live-smoke:
	$(RUN) flowlevels live --symbols $(SYMBOLS) --duration $(DURATION)

levels:
	$(RUN) flowlevels levels --symbol $(SYMBOL) --ts $(TS)

footprint:
	$(RUN) flowlevels footprint --symbol $(SYMBOL) --ts $(TS) --res $(RES)

sanity-plots:
	$(RUN) flowlevels sanity-plots --symbols $(SYMBOLS) --n-days 3 --day 2026-11-17

levels-stats:
	$(RUN) flowlevels levels-stats --symbols $(SYMBOLS) --out docs/sanity/levels-stats-$(TODAY).md

# Phase 3: 60-day tuning-window run, in-sample report. Never runs the hold-out.
backtest:
	nice -n 10 $(RUN) flowlevels backtest --symbols $(SYMBOLS)
