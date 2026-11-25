"""Binance USD-M Futures adapter.

Every endpoint/payload assumption here is listed in docs/DATA_SOURCES.md with its
verification status. Items marked UNVERIFIED there are checked by `flowlevels verify-api`
on the VPS before any real run.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import json
import logging
import random
import re
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import polars as pl

from flowlevels.stream.types import (
    LIQUIDATION_SCHEMA,
    MS_MAX,
    MS_MIN,
    TRADE_SCHEMA,
    Batch,
    aggressor_expr,
    aggressor_from_is_buyer_maker,
    validate_trades,
)
from flowlevels.venues.base import ChecksumMismatchError, DataNotAvailableError

log = logging.getLogger(__name__)

AGGTRADES_CSV_COLUMNS = [
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
]
KLINES_CSV_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
]
REST_AGGTRADES_MAX_LIMIT = 1000  # UNVERIFIED (docs/DATA_SOURCES.md)
_SHA256_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")


def aggtrades_daily_path(symbol: str, day: dt.date) -> str:
    return f"data/futures/um/daily/aggTrades/{symbol}/{symbol}-aggTrades-{day.isoformat()}.zip"


def klines_1m_daily_path(symbol: str, day: dt.date) -> str:
    return f"data/futures/um/daily/klines/{symbol}/1m/{symbol}-1m-{day.isoformat()}.zip"


def parse_checksum_file(text: str) -> str:
    m = _SHA256_RE.search(text)
    if not m:
        raise ChecksumMismatchError(f"No sha256 found in CHECKSUM content: {text[:120]!r}")
    return m.group(1).lower()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_single_csv_from_zip(path: Path) -> bytes:
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"{path}: expected exactly one CSV, found {names}")
        return zf.read(names[0])


def _has_header(data: bytes) -> bool:
    first = data.split(b"\n", 1)[0].split(b",", 1)[0].strip()
    try:
        float(first)
    except ValueError:
        return True
    return False


def parse_aggtrades_csv(data: bytes) -> pl.DataFrame:
    """Parse a data.binance.vision USD-M aggTrades CSV (with or without header)."""
    header = _has_header(data)
    raw = pl.read_csv(
        io.BytesIO(data),
        has_header=header,
        new_columns=AGGTRADES_CSV_COLUMNS,
        schema_overrides={
            "agg_trade_id": pl.Int64,
            "price": pl.Float64,
            "quantity": pl.Float64,
            "first_trade_id": pl.Int64,
            "last_trade_id": pl.Int64,
            "transact_time": pl.Int64,
            "is_buyer_maker": pl.String,
        },
    )
    df = (
        raw.with_columns(pl.col("is_buyer_maker").str.to_lowercase().eq("true"))
        .select(
            pl.col("transact_time").alias("ts_ms"),
            pl.col("agg_trade_id").alias("trade_id"),
            pl.col("price"),
            pl.col("quantity").alias("qty"),
            aggressor_expr("is_buyer_maker"),
            (pl.col("last_trade_id") - pl.col("first_trade_id") + 1).alias("fill_count"),
        )
        .sort("trade_id")
    )
    return validate_trades(df.cast(TRADE_SCHEMA))  # type: ignore[arg-type]


def parse_klines_csv(data: bytes) -> pl.DataFrame:
    header = _has_header(data)
    df = pl.read_csv(
        io.BytesIO(data),
        has_header=header,
        new_columns=KLINES_CSV_COLUMNS,
        schema_overrides={
            "open_time": pl.Int64,
            "close_time": pl.Int64,
            "count": pl.Int64,
            **{
                c: pl.Float64
                for c in (
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "quote_volume",
                    "taker_buy_volume",
                    "taker_buy_quote_volume",
                    "ignore",
                )
            },
        },
    ).drop("ignore")
    ts = df["open_time"]
    if df.height and (int(ts.min()) < MS_MIN or int(ts.max()) >= MS_MAX):  # type: ignore[arg-type]
        raise ValueError("kline open_time not in epoch ms")
    return df.sort("open_time")


def _trade_row_from_payload(d: dict[str, Any]) -> dict[str, Any]:
    """REST/WS aggTrade payload {a,p,q,f,l,T,m} -> TRADE_SCHEMA row."""
    return {
        "ts_ms": int(d["T"]),
        "trade_id": int(d["a"]),
        "price": float(d["p"]),
        "qty": float(d["q"]),
        "aggressor": aggressor_from_is_buyer_maker(bool(d["m"])),
        "fill_count": int(d["l"]) - int(d["f"]) + 1,
    }


def parse_rest_aggtrades(payload: list[dict[str, Any]]) -> pl.DataFrame:
    rows = [_trade_row_from_payload(d) for d in payload]
    df = pl.DataFrame(rows, schema=TRADE_SCHEMA).sort("trade_id")
    return validate_trades(df)


def parse_ws_message(raw: str | bytes) -> Batch | None:
    """Parse a combined-stream frame {"stream":..., "data":{...}} or a raw payload."""
    msg = json.loads(raw)
    data = msg.get("data", msg) if isinstance(msg, dict) else None
    if not isinstance(data, dict):
        return None
    event = data.get("e")
    if event == "aggTrade":
        df = pl.DataFrame([_trade_row_from_payload(data)], schema=TRADE_SCHEMA)
        return Batch(symbol=str(data["s"]), kind="trades", df=df)
    if event == "forceOrder":
        o = data["o"]
        row = {
            "ts_ms": int(o["T"]),
            "side": "B" if o["S"] == "BUY" else "S",
            "price": float(o["p"]),
            "avg_price": float(o["ap"]),
            "qty": float(o["z"]),
            "status": str(o["X"]),
        }
        df = pl.DataFrame([row], schema=LIQUIDATION_SCHEMA)
        return Batch(symbol=str(o["s"]), kind="liquidations", df=df)
    return None


class BinanceUSDM:
    """VenueAdapter for Binance USD-M Futures."""

    name = "binance_usdm"

    def __init__(
        self,
        rest_url: str = "https://fapi.binance.com",
        ws_url: str = "wss://fstream.binance.com",
        vision_url: str = "https://data.binance.vision",
        http: httpx.AsyncClient | None = None,
        max_retries: int = 5,
        retry_base_s: float = 1.0,
    ) -> None:
        self.rest_url = rest_url.rstrip("/")
        self.ws_url = ws_url.rstrip("/")
        self.vision_url = vision_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
        self.max_retries = max_retries
        self.retry_base_s = retry_base_s

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ http helpers
    async def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._http.get(url, **kwargs)
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise
                log.warning("GET %s failed (%s); retry %d", url, exc, attempt + 1)
                await asyncio.sleep(self._backoff(attempt))
                continue
            if resp.status_code == 404:
                raise DataNotAvailableError(url)
            if resp.status_code in (418, 451):
                resp.raise_for_status()  # ban / restricted location: never retry
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_retries:
                    resp.raise_for_status()
                retry_after = float(resp.headers.get("Retry-After", 0) or 0)
                await asyncio.sleep(max(retry_after, self._backoff(attempt)))
                continue
            resp.raise_for_status()
            return resp
        raise AssertionError("unreachable")

    def _backoff(self, attempt: int) -> float:
        base: float = self.retry_base_s * (2**attempt)
        return float(base * (0.5 + random.random()))

    async def _download_verified(self, rel_path: str, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        url = f"{self.vision_url}/{rel_path}"
        dest = dest_dir / rel_path.rsplit("/", 1)[1]
        expected = parse_checksum_file((await self._get(url + ".CHECKSUM")).text)
        if dest.exists() and sha256_file(dest) == expected:
            return dest
        part = dest.with_suffix(dest.suffix + ".part")
        offset = part.stat().st_size if part.exists() else 0
        if offset and not await self._stream_to(url, part, offset):
            part.unlink(missing_ok=True)  # range rejected: restart from scratch
            offset = 0
        if not offset:
            await self._stream_to(url, part, 0)
        actual = sha256_file(part)
        if actual != expected:
            part.unlink(missing_ok=True)
            raise ChecksumMismatchError(f"{url}: sha256 {actual} != {expected}")
        part.replace(dest)
        return dest

    async def _stream_to(self, url: str, part: Path, offset: int) -> bool:
        """Stream url into `part` (resuming at offset). False if the range was refused."""
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        async with self._http.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 404:
                raise DataNotAvailableError(url)
            if offset and resp.status_code != 206:
                return False
            resp.raise_for_status()
            with part.open("ab" if offset else "wb") as f:
                async for chunk in resp.aiter_bytes(1 << 20):
                    f.write(chunk)
        return True

    # ------------------------------------------------------------------ historical
    async def download_trades_day(self, symbol: str, day: dt.date, dest_dir: Path) -> Path:
        return await self._download_verified(aggtrades_daily_path(symbol, day), dest_dir)

    def read_trades_file(self, path: Path) -> pl.DataFrame:
        return parse_aggtrades_csv(_read_single_csv_from_zip(path))

    async def download_klines_1m_day(self, symbol: str, day: dt.date, dest_dir: Path) -> Path:
        return await self._download_verified(klines_1m_daily_path(symbol, day), dest_dir)

    def read_klines_file(self, path: Path) -> pl.DataFrame:
        return parse_klines_csv(_read_single_csv_from_zip(path))

    # ------------------------------------------------------------------ REST
    async def fetch_trades_from_id(
        self, symbol: str, from_id: int, limit: int = REST_AGGTRADES_MAX_LIMIT
    ) -> pl.DataFrame:
        resp = await self._get(
            f"{self.rest_url}/fapi/v1/aggTrades",
            params={"symbol": symbol, "fromId": from_id, "limit": limit},
        )
        payload = resp.json()
        if not isinstance(payload, list):
            raise ValueError(f"Unexpected aggTrades response: {str(payload)[:200]}")
        return parse_rest_aggtrades(payload)

    # ------------------------------------------------------------------ live
    def live_url(self, symbols: list[str], liquidations: bool = True) -> str:
        streams: list[str] = []
        for s in symbols:
            streams.append(f"{s.lower()}@aggTrade")
            if liquidations:
                streams.append(f"{s.lower()}@forceOrder")
        return f"{self.ws_url}/stream?streams={'/'.join(streams)}"

    def parse_live_message(self, raw: str | bytes) -> Batch | None:
        return parse_ws_message(raw)

    async def live_messages(self, url: str) -> AsyncIterator[str | bytes]:
        import websockets

        async with websockets.connect(url, ping_interval=None, max_size=2**22) as ws:
            # Server sends pings; the websockets library replies with pongs automatically.
            async for msg in ws:
                yield msg
