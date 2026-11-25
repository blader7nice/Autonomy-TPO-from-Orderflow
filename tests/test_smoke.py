from pathlib import Path

import pytest

from flowlevels.cli import main
from flowlevels.config import AppConfig, config_hash, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_default_config_loads() -> None:
    cfg = load_config(ROOT / "configs/default.yaml")
    assert set(cfg.symbols) == {"BTCUSDT", "ETHUSDT"}
    assert cfg.symbol("BTCUSDT").bucket == 10.0


def test_config_hash_is_order_independent() -> None:
    a = AppConfig.model_validate(
        {"version": 1, "symbols": {"X": {"exchange_tick": 0.1, "bucket": 1, "tpo_tick": 1}}}
    )
    b = AppConfig.model_validate(
        {"symbols": {"X": {"tpo_tick": 1, "bucket": 1, "exchange_tick": 0.1}}, "version": 1}
    )
    assert config_hash(a) == config_hash(b)


def test_config_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError):
        AppConfig.model_validate({"version": 1, "symbols": {}, "bogus": 1})


def test_bucket_must_be_tick_multiple() -> None:
    with pytest.raises(ValueError):
        AppConfig.model_validate(
            {"version": 1, "symbols": {"X": {"exchange_tick": 0.3, "bucket": 1, "tpo_tick": 1}}}
        )


def test_cli_info(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(ROOT)
    assert main(["info"]) == 0
    assert "config" in capsys.readouterr().out


def test_tpo_tick_must_be_bucket_multiple() -> None:
    with pytest.raises(ValueError, match="tpo_tick"):
        AppConfig.model_validate(
            {"version": 1, "symbols": {"X": {"exchange_tick": 0.1, "bucket": 10, "tpo_tick": 15}}}
        )
