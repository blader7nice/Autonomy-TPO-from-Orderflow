import json

import polars as pl
import pytest

from flowlevels.stream.types import TRADE_SCHEMA, aggressor_from_is_buyer_maker, validate_trades
from flowlevels.venues.base import ChecksumMismatchError
from flowlevels.venues.binance import (
    aggtrades_daily_path,
    klines_1m_daily_path,
    parse_aggtrades_csv,
    parse_checksum_file,
    parse_klines_csv,
    parse_rest_aggtrades,
    parse_ws_message,
)
from tests.helpers import aggtrades_csv, synthetic_trades


def test_aggressor_mapping_buyer_maker_means_seller_aggressor() -> None:
    # Binance m=true: buyer is the maker -> the taker/aggressor is the SELLER.
    assert aggressor_from_is_buyer_maker(True) == "S"
    assert aggressor_from_is_buyer_maker(False) == "B"


def test_csv_aggressor_mapping() -> None:
    data = b"1,100.5,2.0,10,12,1767225600000,true\n2,100.6,1.0,13,13,1767225600001,false\n"
    df = parse_aggtrades_csv(data)
    assert df["aggressor"].to_list() == ["S", "B"]
    assert df["fill_count"].to_list() == [3, 1]
    assert dict(df.schema) == TRADE_SCHEMA


@pytest.mark.parametrize("header", [True, False])
def test_csv_roundtrip_with_and_without_header(header: bool) -> None:
    trades = synthetic_trades(500)
    df = parse_aggtrades_csv(aggtrades_csv(trades, header=header))
    assert df.equals(trades)


def test_csv_rejects_microsecond_timestamps() -> None:
    data = b"1,100.5,2.0,10,12,1767225600000000,true\n"
    with pytest.raises(ValueError, match="epoch ms"):
        parse_aggtrades_csv(data)


def test_ws_aggtrade_mapping() -> None:
    frame = {
        "stream": "btcusdt@aggTrade",
        "data": {
            "e": "aggTrade",
            "E": 1767225600010,
            "s": "BTCUSDT",
            "a": 42,
            "p": "100000.10",
            "q": "0.005",
            "f": 100,
            "l": 101,
            "T": 1767225600000,
            "m": True,
        },
    }
    b = parse_ws_message(json.dumps(frame))
    assert b is not None and b.kind == "trades" and b.symbol == "BTCUSDT"
    row = b.df.row(0, named=True)
    assert row == {
        "ts_ms": 1767225600000,  # trade time T, not event time E
        "trade_id": 42,
        "price": 100000.10,
        "qty": 0.005,
        "aggressor": "S",
        "fill_count": 2,
    }


def test_ws_force_order() -> None:
    frame = {
        "stream": "btcusdt@forceOrder",
        "data": {
            "e": "forceOrder",
            "E": 1767225600100,
            "o": {
                "s": "BTCUSDT",
                "S": "SELL",
                "o": "LIMIT",
                "f": "IOC",
                "q": "0.5",
                "p": "99000",
                "ap": "99100",
                "X": "FILLED",
                "l": "0.5",
                "z": "0.5",
                "T": 1767225600090,
            },
        },
    }
    b = parse_ws_message(json.dumps(frame))
    assert b is not None and b.kind == "liquidations"
    assert b.df.row(0, named=True)["side"] == "S"
    assert b.df.row(0, named=True)["ts_ms"] == 1767225600090


def test_ws_ignores_unknown() -> None:
    assert parse_ws_message('{"result": null, "id": 1}') is None


def test_rest_aggtrades() -> None:
    payload = [
        {"a": 5, "p": "1.5", "q": "2", "f": 1, "l": 1, "T": 1767225600000, "m": False},
        {"a": 6, "p": "1.6", "q": "3", "f": 2, "l": 4, "T": 1767225600001, "m": True},
    ]
    df = parse_rest_aggtrades(payload)
    assert df["aggressor"].to_list() == ["B", "S"]
    assert df["fill_count"].to_list() == [1, 3]


def test_validate_rejects_unsorted_ids() -> None:
    df = synthetic_trades(10).reverse()
    with pytest.raises(ValueError, match="strictly increasing"):
        validate_trades(df)


def test_klines_csv() -> None:
    data = (
        b"1767225600000,1,2,0.5,1.5,10,1767225659999,0,7,4,0,0\n"
        b"1767225660000,1.5,2,1,1.2,5,1767225719999,0,3,1,0,0\n"
    )
    df = parse_klines_csv(data)
    assert df.height == 2 and "ignore" not in df.columns
    assert df["count"].to_list() == [7, 3]


def test_paths_and_checksum() -> None:
    import datetime as dt

    d = dt.date(2026, 1, 2)
    assert aggtrades_daily_path("BTCUSDT", d) == (
        "data/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-01-02.zip"
    )
    assert klines_1m_daily_path("ETHUSDT", d).endswith(
        "klines/ETHUSDT/1m/ETHUSDT-1m-2026-01-02.zip"
    )
    h = "a" * 64
    assert parse_checksum_file(f"{h}  file.zip\n") == h
    assert parse_checksum_file(h.upper()) == h
    with pytest.raises(ChecksumMismatchError):
        parse_checksum_file("nothing here")


def test_trade_schema_columns() -> None:
    assert list(TRADE_SCHEMA) == ["ts_ms", "trade_id", "price", "qty", "aggressor", "fill_count"]
    assert pl.DataFrame(schema=TRADE_SCHEMA).height == 0
