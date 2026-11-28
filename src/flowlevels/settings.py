"""Runtime settings (environment / .env), validated at startup.

Secrets and machine-specific values live here; research/detector parameters live in the
versioned YAML config (see flowlevels.config).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FLOWLEVELS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default=Path("data"), description="Root of the data tree")
    config_path: Path = Field(
        default=Path("configs/default.yaml"), description="Versioned YAML config"
    )
    log_level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR)$")

    # Exchange endpoints (overridable for tests / mirrors).
    binance_rest_url: str = "https://fapi.binance.com"
    # aggTrade/forceOrder/markPrice are "market" streams: they need the routed /market
    # endpoint. Unrouted connections only receive /public streams (VERIFIED 2026-11-26,
    # docs/DATA_SOURCES.md).
    binance_ws_url: str = "wss://fstream.binance.com/market"
    # Share of the 2400/min IP REST weight limit this process may use. The IP is shared
    # with other services on the VPS, so stay well below the limit.
    binance_rest_weight_budget_1m: int = Field(default=1200, ge=20, le=2400)
    binance_vision_url: str = "https://data.binance.vision"

    # Alerts (optional until Phase 5; validated when alerts are enabled).
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # API (Phase 4/6). Bind to localhost by default; never expose unauthenticated.
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8080, ge=1, le=65535)
    # Public (tailnet) URL of the dashboard, used for links in Telegram messages.
    dashboard_url: str | None = None

    @field_validator("binance_ws_url")
    @classmethod
    def _ws_url_must_be_routed(cls, v: str) -> str:
        parts = urlsplit(v)
        if parts.hostname == "fstream.binance.com" and parts.path.rstrip("/") != "/market":
            raise ValueError(
                "Binance USD-M market streams (aggTrade, forceOrder) are only delivered on "
                "the routed endpoint wss://fstream.binance.com/market; unrouted or /public "
                "connections receive no data for them (docs/DATA_SOURCES.md)"
            )
        return v


class SettingsError(RuntimeError):
    pass


def load_settings() -> Settings:
    """Load settings, converting validation errors into one readable message."""
    try:
        return Settings()
    except ValidationError as exc:  # pragma: no cover - message formatting only
        lines = [f"  {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise SettingsError("Invalid FLOWLEVELS settings:\n" + "\n".join(lines)) from exc
