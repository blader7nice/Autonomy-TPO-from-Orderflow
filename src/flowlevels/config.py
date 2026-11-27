"""Versioned YAML config. Every detector/level/research threshold lives here, not in code.

`config_hash()` is a stable hash of the *parsed* config (key order and formatting do not
matter) and must be recorded in every result/report together with the git commit.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Literal

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


class SlippageScenario(_Strict):
    """Adverse slippage per side: `amount` x unit, where unit is the Binance footprint bucket
    of the signal symbol or the tick of the OKX execution instrument."""

    unit: Literal["bucket", "exec_tick"]
    amount: float = Field(ge=0)


class ExecutionInstrument(_Strict):
    venue: str = Field(min_length=1)
    inst_id: str = Field(min_length=1)
    contract_type: str = Field(min_length=1, description="linear | inverse")
    contract_value: float = Field(gt=0)
    contract_value_ccy: str = Field(min_length=1)
    settle_ccy: str = Field(min_length=1)
    tick: float = Field(gt=0)
    min_size_contracts: float = Field(gt=0)
    status: str = Field(min_length=1)
    as_of: str = Field(min_length=1)
    source: str = Field(min_length=1)


class CostsConfig(_Strict):
    """Execution costs. Signals come from Binance USD-M; fees are those of the execution venue.

    `fee_profile` selects one of `fee_profiles` and `slippage` one of `slippage_scenarios`;
    results record both names. The other entries are alternatives/sensitivities.
    """

    fee_profile: str
    fee_profiles: dict[str, FeeProfile]
    slippage: str = "bucket1"
    slippage_scenarios: dict[str, SlippageScenario] = Field(
        default_factory=lambda: {"bucket1": SlippageScenario(unit="bucket", amount=1.0)}
    )
    execution_instruments: dict[str, ExecutionInstrument] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _known_profile(self) -> CostsConfig:
        if self.fee_profile not in self.fee_profiles:
            raise ValueError(
                f"costs.fee_profile {self.fee_profile!r} not in {sorted(self.fee_profiles)}"
            )
        if self.slippage not in self.slippage_scenarios:
            raise ValueError(
                f"costs.slippage {self.slippage!r} not in {sorted(self.slippage_scenarios)}"
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
    repair_holes_from_trades: bool = Field(
        default=True,
        description="Fill holes from the raw trades archive, kline-verified (D-032)",
    )


class AbsorptionConfig(_Strict):
    vol_z_min: float = Field(default=1.5, description="Robust z of bar volume")
    delta_frac_min: float = Field(
        default=0.15, ge=0, le=1, description="Against-direction delta / volume"
    )
    close_pos_min: float = Field(
        default=0.5, ge=0, le=1, description="Close position in the bar range, reversal side"
    )


class ExhaustionConfig(_Strict):
    extreme_frac_max: float = Field(
        default=0.10, gt=0, description="Volume at the extreme bucket / bar POC volume"
    )
    min_buckets: int = Field(default=3, ge=2, description="Minimum ladder length")
    close_pos_min: float = Field(default=0.5, ge=0, le=1)
    lookback_bars: int = Field(default=3, ge=1, description="New extreme vs the last N bars")


class DivergenceConfig(_Strict):
    lookback_bars: int = Field(default=3, ge=1, description="New extreme vs the last N bars")


class TrappedConfig(_Strict):
    delta_z_min: float = Field(default=1.5, description="Robust z of the prior bar's delta")
    close_pos_max: float = Field(
        default=0.33, ge=0, le=1, description="Prior bar closes in this part of its range"
    )


class SignalsConfig(_Strict):
    """Detectors and the confirmation score (SPEC 8). Defaults fixed before any results."""

    resolutions: list[str] = Field(default_factory=lambda: ["1m", "3m", "5m", "15m"])
    primary_resolution: str = "5m"
    window_bars: int = Field(default=4, ge=1, description="W: bars from the touch bar")
    score_threshold: float = Field(default=1.0, gt=0)
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "absorption": 1.0,
            "exhaustion": 1.0,
            "delta_divergence": 1.0,
            "stacked_imbalance": 1.0,
            "trapped": 1.0,
        }
    )
    absorption: AbsorptionConfig = AbsorptionConfig()
    exhaustion: ExhaustionConfig = ExhaustionConfig()
    delta_divergence: DivergenceConfig = DivergenceConfig()
    trapped: TrappedConfig = TrappedConfig()
    dynamic_min_minutes: int = Field(
        default=30, ge=0, description="A dynamic level needs this many closed anchor minutes"
    )


class ContextConfig(_Strict):
    """1h regime label (2 parameters): up/down if the 1h close moved more than
    trend_k_atr x ATR(1h) over lookback_h hours, else range."""

    lookback_h: int = Field(default=24, ge=1)
    trend_k_atr: float = Field(default=2.0, gt=0)


class TradeModelConfig(_Strict):
    stop_buffer_ticks: float = Field(default=2, ge=0, description="Execution-instrument ticks")
    min_stop_k_atr: float = Field(default=0.1, ge=0, description="Minimum stop distance x ATR")
    targets_r: list[float] = Field(default_factory=lambda: [1.0, 2.0, 3.0])
    max_hold_minutes: int = Field(default=240, ge=1, description="Time stop for every exit")
    primary_exit: str = "2R"


class ResearchConfig(_Strict):
    tuning_days: int = Field(default=60, ge=1)
    holdout_days: int = Field(default=274, ge=1, description="About 9 months")
    grid_window_bars: list[int] = Field(default_factory=lambda: [2, 4, 6])
    grid_score_threshold: list[float] = Field(default_factory=lambda: [1.0, 2.0, 3.0])
    min_n_per_group: int = Field(default=30, ge=1)
    bootstrap_reps: int = Field(default=2000, ge=100)
    ci_level: float = Field(default=0.90, gt=0, lt=1)
    power: float = Field(default=0.80, gt=0, lt=1)
    seed: int = 42
    gate_uplift_r: float = 0.15

    @model_validator(mode="after")
    def _grid_size(self) -> ResearchConfig:
        for name in ("grid_window_bars", "grid_score_threshold"):
            if not 1 <= len(getattr(self, name)) <= 3:
                raise ValueError(f"research.{name}: 1-3 values (SPEC 10)")
        return self


class Iteration1Config(_Strict):
    """ITERATION-1 (DECISIONS): confirmation as entry timing for level-to-level trades."""

    resolution: str = "5m"
    grid_score_threshold: list[float] = Field(default_factory=lambda: [2.0, 3.0])
    grid_window_bars: list[int] = Field(default_factory=lambda: [2, 3])
    grid_min_stop_k_atr: list[float] = Field(default_factory=lambda: [0.5, 0.75, 1.0])
    min_stop_cost_mult: float = Field(default=3.0, ge=0, description="x round-trip cost")
    min_target_r: float = Field(default=1.5, gt=0, description="Target distance / stop")
    time_stop_bars: int = Field(default=48, ge=1)
    first_touch_only: bool = True
    min_zone_score: float | None = Field(
        default=None, description="Frozen in-sample median of static zone scores"
    )
    min_treatment_trades: int = Field(default=100, ge=1)
    secondary_exits: list[str] = Field(default_factory=lambda: ["2R", "3R"])


class SupplyZoneConfig(_Strict):
    """Owner's Supply Zone bot (docs/strategies/SUPPLY_ZONE_SPEC.md); values = live code."""

    # bot frames and scan
    n_1h: int = 300
    n_4h: int = 150
    min_1h: int = 100
    min_4h: int = 50
    scan_delay_s: int = 180
    # regime (4h)
    regime_ema_fast: int = 20
    regime_ema_slow: int = 50
    regime_slope_bars: int = 10
    regime_slope_threshold: float = 0.05
    regime_thresholds: dict[str, float] = Field(default_factory=dict)  # by base asset
    regime_threshold_default: float = 0.1
    # core strategy
    recalc_interval: int = 50
    zone_lookback: int = 50
    zone_body_atr: float = 1.2
    zone_tolerance_pct: float = 0.5
    vp_lookback_bars: int = 100
    vp_bins: int = 50
    vp_poc_tolerance_pct: float = 1.5
    fvg_min_gap_pct: float = 0.1
    fvg_max_age_bars: int = 50
    fvg_fill_threshold_pct: float = 80.0
    rsi_period: int = 14
    rsi_short_min: float = 55.0
    rsi_long_max: float = 45.0
    atr_period: int = 14
    min_rr: float = 1.5
    atr_sl_mult: float = 1.5
    atr_tp_mult: float = 3.0
    long_tp_factor: float = 2.0
    # entry filters
    volume_spike_lookback: int = 20
    volume_spike_multiplier: float = 1.5
    local_extreme_lookback: int = 20
    local_extreme_max_age: int = 3
    multiday_extreme_lookback: int = 100
    ema_distance_period: int = 20
    ema_distance_atr_multiplier: float = 3.0
    # engine SL/TP
    sl_atr_period: int = 14
    swing_lookback: int = 20
    sl_zone_buffer_atr: float = 0.3
    sl_swing_buffer_atr: float = 0.2
    tp2_atr_mult: float = 1.5
    partial_tp_min_rr: float = 1.5
    min_rr_required: float = 3.0
    tp1_size: float = 0.33
    # management
    chandelier_lookback: int = 10
    chandelier_atr_mult: float = 2.5
    cooldown_hours: int = 48

    def threshold(self, base: str) -> float:
        return self.regime_thresholds.get(base, self.regime_threshold_default)


class Iteration2Config(_Strict):
    """ITERATION-2 (DECISIONS): Supply Zone trades + orderflow confirmation near entry."""

    resolution: str = "5m"
    grid_score_threshold: list[float] = Field(default_factory=lambda: [1.0, 2.0, 3.0])
    grid_window_bars: list[int] = Field(default_factory=lambda: [2, 3])
    min_confirmed_trades: int = 30
    min_total_trades: int = 30
    gate_expectancy_r: float = 0.0
    gate_uplift_r: float = 0.15


class PlanConfig(_Strict):
    """MASTER-PLAN windows (DECISIONS). Dates inclusive, UTC days."""

    a_start: dt.date = dt.date(2024, 12, 28)
    a_end: dt.date = dt.date(2025, 12, 27)
    holdout_start: dt.date = dt.date(2025, 12, 28)
    holdout_end: dt.date = dt.date(2026, 9, 27)
    b_start: dt.date = dt.date(2026, 9, 28)
    b_end: dt.date = dt.date(2026, 11, 26)
    warmup_days: int = 40
    min_trades: int = 60
    sensitivities: list[str] = Field(
        default_factory=lambda: ["default", "maker_entry", "slip_okx_2tick", "slip_okx_4tick"]
    )


class AppConfig(_Strict):
    version: int = Field(ge=1)
    symbols: dict[str, SymbolConfig]
    retention: RetentionConfig = RetentionConfig()
    footprint: FootprintConfig = FootprintConfig()
    levels: LevelsConfig = LevelsConfig()
    costs: CostsConfig
    data_quality: DataQualityConfig = DataQualityConfig()
    signals: SignalsConfig = SignalsConfig()
    context: ContextConfig = ContextConfig()
    trade_model: TradeModelConfig = TradeModelConfig()
    research: ResearchConfig = ResearchConfig()
    iteration1: Iteration1Config = Iteration1Config()
    supply_zone: SupplyZoneConfig = SupplyZoneConfig()
    iteration2: Iteration2Config = Iteration2Config()
    plan: PlanConfig = PlanConfig()

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
