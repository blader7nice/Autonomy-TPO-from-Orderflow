import datetime as dt
from pathlib import Path

import httpx
import polars as pl
import pytest

from flowlevels.config import load_config
from flowlevels.data.bootstrap import compare_klines, render_bootstrap_md, run_days
from flowlevels.data.pipeline import cross_day_gaps, last_complete_days, process_day
from flowlevels.data.storage import ParquetStore, day_start_ms
from flowlevels.venues.base import ChecksumMismatchError
from flowlevels.venues.binance import BinanceUSDM, aggtrades_daily_path, parse_klines_csv
from tests.helpers import (
    aggtrades_csv,
    checksum_text,
    klines_csv_from_trades,
    synthetic_trades,
    zip_bytes,
)

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "configs/default.yaml")
DAY = dt.date(2026, 1, 1)


class VisionServer:
    """Mock data.binance.vision serving zips + CHECKSUM files (supports Range)."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []
        self.corrupt: set[str] = set()

    def add_day(self, symbol: str, day: dt.date, trades: pl.DataFrame) -> None:
        rel = aggtrades_daily_path(symbol, day)
        name = rel.rsplit("/", 1)[1]
        z = zip_bytes(name.replace(".zip", ".csv"), aggtrades_csv(trades))
        self.files["/" + rel] = z
        self.files["/" + rel + ".CHECKSUM"] = checksum_text(z, name).encode()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = self.files.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        if request.url.path in self.corrupt:
            body = body[:-10] + b"x" * 10
        rng = request.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            return httpx.Response(206, content=body[start:])
        return httpx.Response(200, content=body)


def _adapter(server: VisionServer) -> BinanceUSDM:
    client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    return BinanceUSDM(vision_url="https://vision.test", http=client, retry_base_s=0.0)


def _day_trades(day: dt.date, n: int, first_id: int, seed: int) -> pl.DataFrame:
    return synthetic_trades(
        n, seed=seed, start_ms=day_start_ms(day), span_ms=86_400_000, first_id=first_id
    )


async def test_process_day_end_to_end(tmp_path: Path) -> None:
    server = VisionServer()
    trades = _day_trades(DAY, 5_000, 1, 1)
    server.add_day("BTCUSDT", DAY, trades)
    store = ParquetStore(tmp_path / "data")
    raw = tmp_path / "raw"
    rep = await process_day(_adapter(server), store, CFG, "BTCUSDT", DAY, raw)
    assert rep.status == "done" and rep.n_trades == 5_000 and rep.gaps == []
    assert list(raw.glob("*.zip")) == []  # raw deleted after processing
    oh = store.read("ohlcv_1m", "BTCUSDT", DAY, DAY)
    assert oh["volume"].sum() == pytest.approx(trades["qty"].sum())
    assert oh["first_id"].min() == 1 and oh["last_id"].max() == 5_000
    # Idempotent: second run is skipped without any download.
    n_req = len(server.requests)
    rep2 = await process_day(_adapter(server), store, CFG, "BTCUSDT", DAY, raw)
    assert rep2.status == "skipped" and rep2.last_id == 5_000
    assert len(server.requests) == n_req


async def test_process_day_reports_in_day_gaps(tmp_path: Path) -> None:
    server = VisionServer()
    trades = _day_trades(DAY, 1_000, 1, 2).filter(~pl.col("trade_id").is_in([100, 101, 500]))
    server.add_day("BTCUSDT", DAY, trades)
    rep = await process_day(
        _adapter(server), ParquetStore(tmp_path), CFG, "BTCUSDT", DAY, tmp_path / "raw"
    )
    assert [(g["after_id"], g["before_id"], g["missing"]) for g in rep.gaps] == [
        (99, 102, 2),
        (499, 501, 1),
    ]


async def test_missing_day(tmp_path: Path) -> None:
    rep = await process_day(
        _adapter(VisionServer()), ParquetStore(tmp_path), CFG, "BTCUSDT", DAY, tmp_path / "r"
    )
    assert rep.status == "missing"


async def test_checksum_mismatch_raises_and_cleans_up(tmp_path: Path) -> None:
    server = VisionServer()
    server.add_day("BTCUSDT", DAY, _day_trades(DAY, 100, 1, 3))
    server.corrupt.add("/" + aggtrades_daily_path("BTCUSDT", DAY))
    with pytest.raises(ChecksumMismatchError):
        await process_day(
            _adapter(server), ParquetStore(tmp_path), CFG, "BTCUSDT", DAY, tmp_path / "raw"
        )
    assert list((tmp_path / "raw").iterdir()) == []


async def test_download_resumes_partial_file(tmp_path: Path) -> None:
    server = VisionServer()
    server.add_day("BTCUSDT", DAY, _day_trades(DAY, 2_000, 1, 4))
    rel = aggtrades_daily_path("BTCUSDT", DAY)
    full = server.files["/" + rel]
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / (rel.rsplit("/", 1)[1] + ".part")).write_bytes(full[:1000])
    path = await _adapter(server).download_trades_day("BTCUSDT", DAY, raw)
    assert path.read_bytes() == full
    ranged = [r for r in server.requests if "Range" in r.headers]
    assert ranged and ranged[0].headers["Range"] == "bytes=1000-"


async def test_bootstrap_multi_day_and_cross_day_gaps(tmp_path: Path) -> None:
    server = VisionServer()
    d1, d2, d3 = DAY, DAY + dt.timedelta(days=1), DAY + dt.timedelta(days=2)
    server.add_day("BTCUSDT", d1, _day_trades(d1, 800, 1, 5))
    server.add_day("BTCUSDT", d2, _day_trades(d2, 800, 801, 6))
    server.add_day("BTCUSDT", d3, _day_trades(d3, 800, 1_700, 7))  # 99 ids missing
    store = ParquetStore(tmp_path / "data")
    rep = await run_days(_adapter(server), store, CFG, ["BTCUSDT"], [d1, d2, d3], tmp_path / "raw")
    assert [r["status"] for r in rep["days_report"]] == ["done"] * 3
    assert rep["cross_day_gaps"] == [
        {
            "symbol": "BTCUSDT",
            "after_day": d2.isoformat(),
            "before_day": d3.isoformat(),
            "after_id": 1600,
            "before_id": 1700,
            "missing": 99,
        }
    ]
    assert "Cross-day id gaps" in render_bootstrap_md(rep)
    assert rep["disk_bytes"]["footprint_1m"] > 0 and rep["disk_bytes"]["raw"] == 0
    assert cross_day_gaps([]) == []


def test_compare_klines_exact_match() -> None:
    from flowlevels.footprint.minute import ohlcv_from_trades

    trades = _day_trades(DAY, 3_000, 1, 8)
    kl = parse_klines_csv(klines_csv_from_trades(trades))
    res = compare_klines(ohlcv_from_trades(trades), kl)
    assert res["minutes_mismatched"] == 0
    assert res["minutes_only_aggtrades"] == 0 and res["minutes_only_klines"] == 0
    assert res["max_abs_diff"]["count"] == 0
    # A perturbed kline is detected.
    kl2 = kl.with_columns(
        pl.when(pl.int_range(pl.len()) == 3).then(pl.col("high") + 0.1).otherwise(pl.col("high"))
    )
    assert compare_klines(ohlcv_from_trades(trades), kl2)["minutes_mismatched"] == 1


def test_last_complete_days() -> None:
    days = last_complete_days(3, today=dt.date(2026, 3, 1))
    assert days == [dt.date(2026, 2, 26), dt.date(2026, 2, 27), dt.date(2026, 2, 28)]


def test_compare_klines_classifies_boundary_shift_vs_unexplained() -> None:
    from flowlevels.footprint.minute import ohlcv_from_trades

    trades = _day_trades(DAY, 3_000, 1, 8)
    ohlcv = ohlcv_from_trades(trades)
    kl = parse_klines_csv(klines_csv_from_trades(trades))
    # Boundary shift: 0.001 of buyer volume and one fill move from minute 3 to minute 4 in
    # the klines (an aggTrade straddling the boundary). Minutes 3 and 4 both mismatch but
    # net to zero.
    i = pl.int_range(pl.len())
    shift = pl.when(i == 3).then(-0.001).when(i == 4).then(0.001).otherwise(0.0)
    cshift = pl.when(i == 3).then(-1).when(i == 4).then(1).otherwise(0)
    kl_b = kl.with_columns(
        pl.col("volume") + shift,
        pl.col("taker_buy_volume") + shift,
        pl.col("count") + cshift,
    )
    res = compare_klines(ohlcv, kl_b)
    assert res["minutes_mismatched"] == 2
    assert res["minutes_mismatched_boundary"] == 2 and res["minutes_mismatched_unexplained"] == 0
    assert res["day_totals"]["volume"][0] == pytest.approx(res["day_totals"]["volume"][1])
    # Volume missing from one minute with no compensating neighbour is unexplained.
    kl_u = kl.with_columns(
        pl.when(i == 10).then(pl.col("volume") + 1.0).otherwise(pl.col("volume"))
    )
    res_u = compare_klines(ohlcv, kl_u)
    assert res_u["minutes_mismatched_unexplained"] == 1
    assert res_u["unexplained_minute_ts"] == [int(kl["open_time"][10])]
