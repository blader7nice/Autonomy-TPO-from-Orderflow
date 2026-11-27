from pathlib import Path

import pytest

from flowlevels.cli import main
from flowlevels.config import AppConfig, config_hash, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_default_config_loads() -> None:
    cfg = load_config(ROOT / "configs/default.yaml")
    assert set(cfg.symbols) == {"BTCUSDT", "ETHUSDT"}
    assert cfg.symbol("BTCUSDT").bucket == 10.0


_FEES = {"venue": "v", "taker_fee": 0.0005, "maker_fee": 0.0002, "tier": "t", "as_of": "d"}
COSTS = {"fee_profile": "p", "fee_profiles": {"p": {**_FEES, "source": "s"}}}


def test_config_hash_is_order_independent() -> None:
    a = AppConfig.model_validate(
        {
            "version": 1,
            "symbols": {"X": {"exchange_tick": 0.1, "bucket": 1, "tpo_tick": 1}},
            "costs": COSTS,
        }
    )
    b = AppConfig.model_validate(
        {
            "costs": COSTS,
            "symbols": {"X": {"tpo_tick": 1, "bucket": 1, "exchange_tick": 0.1}},
            "version": 1,
        }
    )
    assert config_hash(a) == config_hash(b)


def test_costs_are_required_and_profile_must_exist() -> None:
    sym = {"X": {"exchange_tick": 0.1, "bucket": 1, "tpo_tick": 1}}
    with pytest.raises(ValueError, match="costs"):
        AppConfig.model_validate({"version": 1, "symbols": sym})
    with pytest.raises(ValueError, match="fee_profile"):
        AppConfig.model_validate(
            {"version": 1, "symbols": sym, "costs": {**COSTS, "fee_profile": "nope"}}
        )


def test_default_cost_model_is_okx_with_binance_alternative() -> None:
    costs = load_config(ROOT / "configs/default.yaml").costs
    assert costs.fee_profile == "okx_eu_inverse" and costs.slippage == "bucket1"
    assert costs.fees.venue == "OKX EEA" and "UNCONFIRMED" in costs.fees.source
    assert costs.slippage_scenarios["okx_4tick"].amount == 4
    btc = costs.execution_instruments["BTCUSDT"]
    assert (btc.inst_id, btc.contract_type, btc.tick) == ("BTC-USD-SWAP", "inverse", 0.1)
    assert (costs.maker_fee, costs.taker_fee) == (0.0002, 0.0005)
    assert costs.fee_profiles["binance_regular"].venue == "Binance"


def test_zone_width_must_exceed_tolerance() -> None:
    from flowlevels.config import LevelsConfig

    with pytest.raises(ValueError, match="zone_max_width"):
        LevelsConfig(zone_tolerance_k_atr=0.3, zone_max_width_k_atr=0.5)


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
