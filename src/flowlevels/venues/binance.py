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
import itertools
import json
import logging
import random
import re
import time
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
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
from flowlevels.venues.base import (
    ChecksumMismatchError,
    DataNotAvailableError,
    RestLookbackExceededError,
)

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
# VERIFIED 2026-11-26 (docs/DATA_SOURCES.md): limit 1001 -> HTTP 400 code -1130.
REST_AGGTRADES_MAX_LIMIT = 1000
# VERIFIED 2026-11-26: GET /fapi/v1/aggTrades has request weight 20 (official SDK docstring,
# change log; the X-MBX-USED-WEIGHT-1M header grows by 20 per call).
REST_AGGTRADES_WEIGHT = 20
# VERIFIED 2026-11-26: fromId older than 48 h -> HTTP 400 code -4166
# "Search window is restricted to recent 2 days only."
ERR_SEARCH_WINDOW = -4166
USED_WEIGHT_HEADER = "x-mbx-used-weight-1m"
_SHA256_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")


def aggtrades_daily_path(symbol: str, day: dt.date) -> str:
    return f"data/futures/um/daily/aggTrades/{symbol}/{symbol}-aggTrades-{day.isoformat()}.zip"


def raw_trades_daily_path(symbol: str, day: dt.date) -> str:
    """Raw (non-aggregated) trades archive. Columns id,price,qty,quote_qty,time,is_buyer_maker
    with a header; row count equals the kline `count` (VERIFIED 2026-11-26)."""
    return f"data/futures/um/daily/trades/{symbol}/{symbol}-trades-{day.isoformat()}.zip"


def klines_1m_daily_path(symbol: str, day: dt.date) -> str:
    return f"data/futures/um/daily/klines/{symbol}/1m/{symbol}-1m-{day.isoformat()}.zip"


def klines_1m_monthly_path(symbol: str, year: int, month: int) -> str:
    """Monthly 1m klines archive (same CSV format as the daily files)."""
    return f"data/futures/um/monthly/klines/{symbol}/1m/{symbol}-1m-{year:04d}-{month:02d}.zip"


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


def iter_aggtrades_zip(path: Path, chunk_rows: int) -> Iterator[pl.DataFrame]:
    """Stream an aggTrades zip in batches of <= chunk_rows CSV lines (decompressed on the
    fly, never fully in memory). Each batch is parsed like `parse_aggtrades_csv`; the header
    (first line only) is detected per batch. Batches must continue the trade_id order."""
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"{path}: expected exactly one CSV, found {names}")
        last_id: int | None = None
        with zf.open(names[0]) as f:
            while True:
                lines = list(itertools.islice(f, chunk_rows))
                if not lines:
                    break
                lines = [ln for ln in lines if ln.strip()]
                if not lines:
                    continue
                df = parse_aggtrades_csv(b"".join(lines))
                del lines
                if df.height == 0:
                    continue
                if last_id is not None and int(df["trade_id"][0]) <= last_id:
                    raise ValueError(f"{path.name}: trade ids not increasing across batches")
                last_id = int(df["trade_id"][-1])
                yield df


def _has_header(data: bytes) -> bool:
    first = data.split(b"\n", 1)[0].split(b",", 1)[0].strip()
    try:
        float(first)
    except ValueError:
        return True
    return False


def parse_aggtrades_csv(data: bytes, keep_fill_ids: bool = False) -> pl.DataFrame:
    """Parse a data.binance.vision USD-M aggTrades CSV (with or without header).

    With keep_fill_ids the raw fill-id range [first_fill_id, last_fill_id] is kept as two
    extra columns (used to locate archive holes in the raw `trades` archive, D-032).
    """
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
            pl.col("first_trade_id").alias("first_fill_id"),
            pl.col("last_trade_id").alias("last_fill_id"),
        )
        .sort("trade_id")
    )
    trades = validate_trades(df.select(list(TRADE_SCHEMA)).cast(TRADE_SCHEMA))  # type: ignore[arg-type]
    if keep_fill_ids:
        return trades.with_columns(df["first_fill_id"], df["last_fill_id"])
    return trades


RAW_TRADES_CSV_COLUMNS = ["id", "price", "qty", "quote_qty", "time", "is_buyer_maker"]


def parse_raw_trades_csv(data: bytes) -> pl.DataFrame:
    """Raw `trades` archive CSV -> TRADE_SCHEMA, one row per fill (fill_count = 1).

    `trade_id` is the raw fill id, a different id namespace from aggTrade ids: use these rows
    only for hole repair (D-032), never mixed into an aggTrade id sequence. `is_buyer_maker`
    has the same meaning as aggTrade `m` (true => the aggressor is the SELLER).
    """
    raw = pl.read_csv(
        io.BytesIO(data),
        has_header=_has_header(data),
        new_columns=RAW_TRADES_CSV_COLUMNS,
        schema_overrides={
            "id": pl.Int64,
            "price": pl.Float64,
            "qty": pl.Float64,
            "quote_qty": pl.Float64,
            "time": pl.Int64,
            "is_buyer_maker": pl.String,
        },
    )
    df = (
        raw.with_columns(pl.col("is_buyer_maker").str.to_lowercase().eq("true"))
        .select(
            pl.col("time").alias("ts_ms"),
            pl.col("id").alias("trade_id"),
            pl.col("price"),
            pl.col("qty"),
            aggressor_expr("is_buyer_maker"),
            pl.lit(1, dtype=pl.Int64).alias("fill_count"),
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


def _error_code(resp: httpx.Response) -> int | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return int(code) if isinstance(code, int) else None


def _trade_row_from_payload(d: dict[str, Any]) -> dict[str, Any]:
    """REST/WS aggTrade payload {a,p,q,f,l,T,m} -> TRADE_SCHEMA row.

    `q` includes trades against RPI orders; `nq` (WS/REST since 2025-12-31) excludes them
    and `st` is the symbol type. Both are ignored: the data.binance.vision CSV only has the
    RPI-inclusive quantity, and history and live must share one definition (D-020).
    """
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


def parse_rest_klines(payload: Any) -> pl.DataFrame:
    """REST kline rows [open_time, open, high, low, close, volume, close_time, ...]."""
    if not isinstance(payload, list):
        raise ValueError(f"Unexpected klines response: {str(payload)[:200]}")
    schema = {"minute_ts": pl.Int64, "open": pl.Float64, "high": pl.Float64,
              "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64}  # fmt: skip
    rows = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
            for r in payload]  # fmt: skip
    return pl.DataFrame(rows, schema=schema, orient="row")


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
        ws_url: str = "wss://fstream.binance.com/market",
        vision_url: str = "https://data.binance.vision",
        http: httpx.AsyncClient | None = None,
        max_retries: int = 5,
        retry_base_s: float = 1.0,
        weight_budget_1m: int = 1200,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.rest_url = rest_url.rstrip("/")
        self.ws_url = ws_url.rstrip("/")
        self.vision_url = vision_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
        self.max_retries = max_retries
        self.retry_base_s = retry_base_s
        # Operational rate limiting only (never market logic): the local wall clock is
        # used to wait for the next 1-minute weight window.
        self.weight_budget_1m = weight_budget_1m
        self._sleep = sleep
        self._wall_clock = wall_clock
        self.last_used_weight_1m: int | None = None
        self.weight_waits = 0

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
                await self._sleep(self._backoff(attempt))
                continue
            if resp.status_code == 404:
                raise DataNotAvailableError(url)
            if resp.status_code == 400 and _error_code(resp) == ERR_SEARCH_WINDOW:
                raise RestLookbackExceededError(f"{url}: {resp.text[:200]}")
            if resp.status_code in (418, 451):
                resp.raise_for_status()  # ban / restricted location: never retry
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_retries:
                    resp.raise_for_status()
                retry_after = float(resp.headers.get("Retry-After", 0) or 0)
                await self._sleep(max(retry_after, self._backoff(attempt)))
                continue
            resp.raise_for_status()
            await self._respect_weight(resp)
            return resp
        raise AssertionError("unreachable")

    async def _respect_weight(self, resp: httpx.Response) -> None:
        """Pause until the next 1-minute window when the IP's used weight reaches the budget.

        The weight limit is per IP and shared with every other process on this host, so
        this process stops at `weight_budget_1m` (default: half of the 2400 limit).
        """
        raw = resp.headers.get(USED_WEIGHT_HEADER)
        if raw is None:
            return
        used = int(raw)
        self.last_used_weight_1m = used
        if used + REST_AGGTRADES_WEIGHT > self.weight_budget_1m:
            wait = 60.0 - (self._wall_clock() % 60.0) + 1.0
            self.weight_waits += 1
            log.warning(
                "REST used weight %d near budget %d; pausing %.1fs",
                used,
                self.weight_budget_1m,
                wait,
            )
            await self._sleep(wait)

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

    def read_trades_file(self, path: Path, keep_fill_ids: bool = False) -> pl.DataFrame:
        return parse_aggtrades_csv(_read_single_csv_from_zip(path), keep_fill_ids)

    def iter_trades_file(self, path: Path, chunk_rows: int) -> Iterator[pl.DataFrame]:
        return iter_aggtrades_zip(path, chunk_rows)

    async def download_raw_trades_day(self, symbol: str, day: dt.date, dest_dir: Path) -> Path:
        return await self._download_verified(raw_trades_daily_path(symbol, day), dest_dir)

    def read_raw_trades_file(self, path: Path) -> pl.DataFrame:
        return parse_raw_trades_csv(_read_single_csv_from_zip(path))

    async def download_klines_1m_day(self, symbol: str, day: dt.date, dest_dir: Path) -> Path:
        return await self._download_verified(klines_1m_daily_path(symbol, day), dest_dir)

    async def download_klines_1m_month(
        self, symbol: str, year: int, month: int, dest_dir: Path
    ) -> Path:
        return await self._download_verified(klines_1m_monthly_path(symbol, year, month), dest_dir)

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

    async def fetch_klines_1m(
        self, symbol: str, start_ms: int | None = None, limit: int = 5
    ) -> pl.DataFrame:
        """REST `/fapi/v1/klines` 1m, oldest first, *including* the forming candle when the
        range reaches the present (tested live 2026-11-27, DECISIONS PAPER-PLAN). Weight 1 for
        limit < 100, 2 below 500, 5 up to 1000."""
        params: dict[str, Any] = {"symbol": symbol, "interval": "1m", "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        resp = await self._get(f"{self.rest_url}/fapi/v1/klines", params=params)
        return parse_rest_klines(resp.json())

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
            # VERIFIED 2026-11-26: the server pings every 3 min and disconnects if no pong
            # arrives within 10 min; the websockets library answers pings automatically.
            async for msg in ws:
                yield msg
