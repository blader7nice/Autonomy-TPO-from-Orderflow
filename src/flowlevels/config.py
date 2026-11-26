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
    hvn_min_prominence: float = Field(
        default=0.05,
        ge=0,
        description="Minimum peak/valley prominence as a fraction of the smoothed profile max",
    )
    hvn_min_distance_k_atr: float = Field(
        default=0.5, ge=0, description="Minimum distance between nodes of one kind, x ATR(1h)"
    )
    hvn_top_n: int = Field(default=3, ge=0, description="Max HVNs per profile, by prominence")
    lvn_top_n: int = Field(default=3, ge=0, description="Max LVNs per profile, by prominence")
    atr_period_1h: int = Field(default=14, ge=1)
    confluence_k_atr: float = Field(
        default=0.25, gt=0, description="Max distance of a member from the zone anchor, x ATR"
    )
    zone_tolerance_k_atr: float = Field(default=0.10, ge=0)
    zone_max_width_k_atr: float = Field(
        default=0.5, gt=0, description="Hard cap on zone width (high - low incl. tolerance), x ATR"
    )
    weights: dict[str, float] = Field(default_factory=dict, description="Level-type weights")

    @model_validator(mode="after")
    def _zone_width(self) -> LevelsConfig:
        if self.zone_max_width_k_atr <= 2 * self.zone_tolerance_k_atr:
            raise ValueError("zone_max_width_k_atr must exceed 2 x zone_tolerance_k_atr")
        return self

    def weight(self, level_type: str) -> float:
        return self.weights.get(level_type, 1.0)


class FeeProfile(_Strict):
    venue: str = Field(min_length=1, description="Execution venue the fees apply to")
    taker_fee: float = Field(ge=0, description="Fraction of notional per side")
    maker_fee: float = Field(description="Fraction of notional per side (negative = rebate)")
    tier: str = Field(min_length=1)
    as_of: str = Field(min_length=1, description="Date the rates were checked (YYYY-MM-DD)")
    source: str = Field(min_length=1, description="Where the rates come from")


class CostsConfig(_Strict):
    """Execution costs. Signals come from Binance USD-M; fees are those of the execution venue.

    `fee_profile` selects one of `fee_profiles`; results record the profile name.
    """

    fee_profile: str
    fee_profiles: dict[str, FeeProfile]
    slippage_buckets: float = Field(default=1.0, ge=0)

    @model_validator(mode="after")
    def _known_profile(self) -> CostsConfig:
        if self.fee_profile not in self.fee_profiles:
            raise ValueError(
                f"costs.fee_profile {self.fee_profile!r} not in {sorted(self.fee_profiles)}"
            )
        return self

    @property
    def fees(self) -> FeeProfile:
        return self.fee_profiles[self.fee_profile]

    @property
    def taker_fee(self) -> float:
        return self.fees.taker_fee

    @property
    def maker_fee(self) -> float:
        return self.fees.maker_fee


class DataQualityConfig(_Strict):
    max_trade_silence_s: float = Field(
        default=60.0,
        gt=0,
        description="A stretch without any trade longer than this is recorded as a data hole",
    )


class AppConfig(_Strict):
    version: int = Field(ge=1)
    symbols: dict[str, SymbolConfig]
    retention: RetentionConfig = RetentionConfig()
    footprint: FootprintConfig = FootprintConfig()
    levels: LevelsConfig = LevelsConfig()
    costs: CostsConfig
    data_quality: DataQualityConfig = DataQualityConfig()

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
