"""Hole repair from the raw trades archive (D-032), on a synthetic tape."""

import datetime as dt
from pathlib import Path

import httpx
import numpy as np
import polars as pl
import pytest

from flowlevels.config import load_config
from flowlevels.data.pipeline import process_day
from flowlevels.data.quality import load_holes, load_repairs
from flowlevels.data.storage import ParquetStore, day_start_ms
from flowlevels.footprint.minute import footprint_from_trades, ohlcv_from_trades
from flowlevels.stream.types import TRADE_SCHEMA
from flowlevels.venues.binance import (
    BinanceUSDM,
    aggtrades_daily_path,
    klines_1m_daily_path,
    parse_raw_trades_csv,
    raw_trades_daily_path,
)
from tests.helpers import checksum_text, klines_csv_from_trades, zip_bytes
from tests.test_pipeline import VisionServer

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "configs/default.yaml")
DAY = dt.date(2026, 1, 2)
D0 = day_start_ms(DAY)
HOLE = (D0 + 3_600_000 + 20_000, D0 + 3_600_000 + 400_000)  # 01:00:20 .. 01:06:40


def _fills(n: int = 40_000, seed: int = 3) -> pl.DataFrame:
    """Per-fill tape over the whole day, one fill every ~2 s, ids from 500."""
    rng = np.random.default_rng(seed)
    ts = np.sort(rng.integers(D0, D0 + 86_400_000, size=n))
    price = np.round(100_000 + np.cumsum(rng.choice([-1, 0, 1], size=n)) * 0.1, 1)
    return pl.DataFrame(
        {
            "ts_ms": ts.astype(np.int64),
            "trade_id": np.arange(500, 500 + n, dtype=np.int64),
            "price": price,
            "qty": np.round(rng.exponential(0.05, n) + 0.001, 3),
            "aggressor": np.where(rng.random(n) < 0.5, "B", "S"),
            "fill_count": np.ones(n, dtype=np.int64),
        },
        schema=TRADE_SCHEMA,
    )


def _agg_csv(fills: pl.DataFrame) -> bytes:
    """One aggTrade per fill, with the hole cut out but aggTrade ids kept contiguous."""
    keep = fills.filter((pl.col("ts_ms") <= HOLE[0]) | (pl.col("ts_ms") >= HOLE[1]))
    rows = [
        f"{i + 1},{r['price']},{r['qty']},{r['trade_id']},{r['trade_id']},{r['ts_ms']},"
        f"{'true' if r['aggressor'] == 'S' else 'false'}"
        for i, r in enumerate(keep.iter_rows(named=True))
    ]
    head = "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker"
    return ("\n".join([head, *rows]) + "\n").encode()


def _raw_csv(fills: pl.DataFrame) -> bytes:
    rows = [
        f"{r['trade_id']},{r['price']},{r['qty']},0,{r['ts_ms']},"
        f"{'true' if r['aggressor'] == 'S' else 'false'}"
        for r in fills.iter_rows(named=True)
    ]
    return ("\n".join(["id,price,qty,quote_qty,time,is_buyer_maker", *rows]) + "\n").encode()


def _put(server: VisionServer, rel: str, csv: bytes) -> None:
    name = rel.rsplit("/", 1)[1]
    z = zip_bytes(name.replace(".zip", ".csv"), csv)
    server.files["/" + rel] = z
    server.files["/" + rel + ".CHECKSUM"] = checksum_text(z, name).encode()


def _server(fills: pl.DataFrame, raw: pl.DataFrame | None = None) -> VisionServer:
    s = VisionServer()
    _put(s, aggtrades_daily_path("BTCUSDT", DAY), _agg_csv(fills))
    _put(s, klines_1m_daily_path("BTCUSDT", DAY), klines_csv_from_trades(fills))
    _put(s, raw_trades_daily_path("BTCUSDT", DAY), _raw_csv(fills if raw is None else raw))
    return s


def _adapter(server: VisionServer) -> BinanceUSDM:
    client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    return BinanceUSDM(vision_url="https://vision.test", http=client, retry_base_s=0.0)


def test_raw_trades_aggressor_mapping() -> None:
    df = parse_raw_trades_csv(
        b"id,price,qty,quote_qty,time,is_buyer_maker\n"
        b"7,100.0,1.0,100.0,1767225600000,true\n8,100.1,2.0,200.2,1767225600001,false\n"
    )
    assert df["aggressor"].to_list() == ["S", "B"]  # m=true => seller is the aggressor
    assert df["fill_count"].to_list() == [1, 1]


async def test_hole_is_repaired_and_matches_full_tape(tmp_path: Path) -> None:
    fills = _fills()
    store = ParquetStore(tmp_path / "data")
    rep = await process_day(_adapter(_server(fills)), store, CFG, "BTCUSDT", DAY, tmp_path / "raw")
    assert rep.status == "done" and rep.holes == [] and len(rep.repairs) == 1
    r = rep.repairs[0]
    assert r["source_file"] == "BTCUSDT-trades-2026-01-02.zip" and len(r["source_sha256"]) == 64
    assert r["check"]["interior_mismatched"] == 0 and r["day_totals"]["ok"]
    assert load_holes(store, "BTCUSDT") == [] and len(load_repairs(store, "BTCUSDT")) == 1
    assert list((tmp_path / "raw").glob("*.zip")) == []
    # Volume/footprint equal a build from the complete per-fill tape.
    oh = store.read("ohlcv_1m", "BTCUSDT", DAY, DAY).sort("minute_ts")
    full = ohlcv_from_trades(fills)
    for c in ("open", "high", "low", "close", "volume", "buy_volume", "sell_volume", "sum_pv"):
        np.testing.assert_allclose(oh[c].to_numpy(), full[c].to_numpy(), rtol=1e-12)
    fp = store.read("footprint_1m", "BTCUSDT", DAY, DAY).sort("minute_ts", "bucket")
    ffull = footprint_from_trades(fills, 0.1, 10.0)
    assert fp.height == ffull.height
    np.testing.assert_allclose(fp["ask_vol"].to_numpy(), ffull["ask_vol"].to_numpy(), rtol=1e-12)
    # Minutes with fills only carry no aggTrade ids.
    inner = oh.filter((pl.col("minute_ts") > HOLE[0]) & (pl.col("minute_ts") + 60_000 < HOLE[1]))
    assert inner.height > 0 and inner["first_id"].null_count() == inner.height


async def test_failed_check_keeps_hole_as_exclusion(tmp_path: Path) -> None:
    fills = _fills()
    bad = fills.with_columns(  # raw archive disagrees with klines inside the hole
        pl.when((pl.col("ts_ms") > HOLE[0]) & (pl.col("ts_ms") < HOLE[1]))
        .then(pl.col("qty") * 2)
        .otherwise(pl.col("qty"))
        .alias("qty")
    )
    store = ParquetStore(tmp_path / "data")
    rep = await process_day(
        _adapter(_server(fills, raw=bad)), store, CFG, "BTCUSDT", DAY, tmp_path / "raw"
    )
    assert rep.repairs == [] and len(rep.holes) == 1
    assert rep.holes[0]["repair_error"] == "kline check failed"
    assert len(load_holes(store, "BTCUSDT")) == 1  # the exclusion fallback applies


async def test_inconsistent_raw_archive_keeps_hole(tmp_path: Path) -> None:
    """Reused trade ids in the raw archive (seen on ETHUSDT 2025-08-29): no repair, no crash."""
    fills = _fills()
    dup = pl.concat([fills.head(1000), fills.slice(999, 1), fills.slice(1000)])
    store = ParquetStore(tmp_path / "data")
    rep = await process_day(
        _adapter(_server(fills, raw=dup)), store, CFG, "BTCUSDT", DAY, tmp_path / "raw"
    )
    assert rep.status == "done" and rep.repairs == [] and len(rep.holes) == 1
    assert rep.holes[0]["repair_error"].startswith("source inconsistent")
    assert len(load_holes(store, "BTCUSDT")) == 1
    assert list((tmp_path / "raw").glob("*.zip")) == []


async def test_complete_day_supersedes_live_parts(tmp_path: Path) -> None:
    fills = _fills(5_000)
    store = ParquetStore(tmp_path / "data")
    live = ohlcv_from_trades(fills.head(100))
    store.write_part("ohlcv_1m", "BTCUSDT", DAY, "live-00-x", live)
    await process_day(_adapter(_server(fills)), store, CFG, "BTCUSDT", DAY, tmp_path / "raw")
    names = [p.name for p in store.partition("ohlcv_1m", "BTCUSDT", DAY).glob("*.parquet")]
    assert names == ["hist.parquet"]
    oh = store.read("ohlcv_1m", "BTCUSDT", DAY, DAY)
    assert oh["volume"].sum() == pytest.approx(fills["qty"].sum())
