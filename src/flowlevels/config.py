"""Versioned YAML config. Every detector/level/research threshold lives here, not in code.

`config_hash()` is a stable hash of the *parsed* config (key order and formatting do not
matter) and must be recorded in every result/report together with the git commit.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SymbolConfig(_Strict):
    exchange_tick: float = Field(gt=0, description="Exchange price tick (price filter)")
    bucket: float = Field(gt=0, description="Footprint price bucket size")
    tpo_tick: float = Field(gt=0, description="TPO / volume-profile row size")

    @model_validator(mode="after")
    def _bucket_multiple(self) -> SymbolConfig:
        for name in ("bucket", "tpo_tick"):
            ratio = getattr(self, name) / self.exchange_tick
            if abs(ratio - round(ratio)) > 1e-9:
                raise ValueError(f"{name} must be a multiple of exchange_tick")
        r = self.tpo_tick / self.bucket
        if r < 1 - 1e-9 or abs(r - round(r)) > 1e-9:
            raise ValueError("tpo_tick must be a multiple of bucket (volume profile rows)")
        return self


class RetentionConfig(_Strict):
    raw_live_ticks_days: int = Field(default=7, ge=1)
    footprint_1m_days: int = Field(default=30, ge=1)


class FootprintConfig(_Strict):
    imb_ratio: float = Field(default=3.0, gt=1)
    imb_min_volume: float = Field(default=0.0, ge=0, description="Min volume (base units)")
    stack_n: int = Field(default=3, ge=2)
    baseline_days: int = Field(default=20, ge=1)
    baseline_slot_minutes: int = Field(default=30, ge=1)
    baseline_min_samples: int = Field(default=5, ge=1)


class LevelsConfig(_Strict):
    tpo_period_minutes: int = Field(default=30, ge=1)
    value_area_pct: float = Field(default=0.70, gt=0, lt=1)
    naked_poc_lookback_days: int = Field(default=10, ge=1)
    hvn_smoothing_rows: int = Field(default=5, ge=1, description="Odd moving-average window")
    hvn_prominence: float = Field(default=0.10, ge=0, description="Fraction of max volume")
    atr_period_1h: int = Field(default=14, ge=1)
    confluence_k_atr: float = Field(default=0.25, gt=0)
    zone_tolerance_k_atr: float = Field(default=0.15, ge=0)
    weights: dict[str, float] = Field(default_factory=dict, description="Level-type weights")

    def weight(self, level_type: str) -> float:
        return self.weights.get(level_type, 1.0)


class CostsConfig(_Strict):
    taker_fee: float = Field(default=0.0005, ge=0)
    maker_fee: float = Field(default=0.0002, ge=0)
    slippage_buckets: float = Field(default=1.0, ge=0)


class AppConfig(_Strict):
    version: int = Field(ge=1)
    symbols: dict[str, SymbolConfig]
    retention: RetentionConfig = RetentionConfig()
    footprint: FootprintConfig = FootprintConfig()
    levels: LevelsConfig = LevelsConfig()
    costs: CostsConfig = CostsConfig()

    def symbol(self, name: str) -> SymbolConfig:
        try:
            return self.symbols[name]
        except KeyError as exc:
            raise KeyError(f"Symbol {name!r} not in config ({sorted(self.symbols)})") from exc


def load_config(path: Path | str) -> AppConfig:
    raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Config {path} must be a mapping")
    return AppConfig.model_validate(raw)


def config_hash(cfg: AppConfig) -> str:
    canonical = json.dumps(cfg.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def git_commit(cwd: Path | None = None) -> str:
    """Current git commit (short) with '-dirty' suffix; 'unknown' outside a repo."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
