"""`flowlevels verify-api`: check every UNVERIFIED assumption against the real endpoints.

Runs on the VPS (needs network). Prints PASS/FAIL per check with the observed values so
docs/DATA_SOURCES.md can be updated with dated evidence.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from flowlevels.stream.types import MS_MAX, MS_MIN
from flowlevels.venues.binance import (
    AGGTRADES_CSV_COLUMNS,
    BinanceUSDM,
    _has_header,
    aggtrades_daily_path,
    klines_1m_daily_path,
    parse_checksum_file,
    parse_ws_message,
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)


async def _check_vision(
    adapter: BinanceUSDM, symbol: str, day: dt.date, tmp: Path, kind: str
) -> Check:
    rel = (
        aggtrades_daily_path(symbol, day)
        if kind == "aggTrades"
        else klines_1m_daily_path(symbol, day)
    )
    name = f"vision_{kind}"
    try:
        cs_text = (await adapter._get(f"{adapter.vision_url}/{rel}.CHECKSUM")).text
        path = await adapter._download_verified(rel, tmp)
    except Exception as exc:
        return Check(name, False, {"url": rel, "error": repr(exc)})
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        data = zf.read(names[0])
    first_line = data.split(b"\n", 1)[0].decode(errors="replace")
    detail: dict[str, Any] = {
        "url": rel,
        "checksum_file": cs_text.strip()[:200],
        "checksum_parsed": parse_checksum_file(cs_text),
        "zip_members": names,
        "has_header": _has_header(data),
        "first_line": first_line[:200],
        "n_columns": first_line.count(",") + 1,
    }
    ok = True
    if kind == "aggTrades":
        df = adapter.read_trades_file(path)
        ts0 = int(df["ts_ms"][0])
        detail.update(
            rows=df.height,
            ts_first=ts0,
            ts_unit_ms=MS_MIN <= ts0 < MS_MAX,
            ids_contiguous=bool((df["trade_id"].diff().drop_nulls() == 1).all()),
        )
        ok = detail["n_columns"] == len(AGGTRADES_CSV_COLUMNS) and detail["ts_unit_ms"]
    else:
        kl = adapter.read_klines_file(path)
        detail.update(rows=kl.height)
        ok = kl.height == 1440
    path.unlink(missing_ok=True)
    return Check(name, ok, detail)


async def _check_rest(adapter: BinanceUSDM, symbol: str, from_id: int) -> Check:
    try:
        resp = await adapter._get(
            f"{adapter.rest_url}/fapi/v1/aggTrades",
            params={"symbol": symbol, "fromId": from_id, "limit": 1000},
        )
        payload = resp.json()
    except Exception as exc:
        return Check("rest_aggTrades", False, {"error": repr(exc)})
    keys = sorted(payload[0].keys()) if payload else []
    df = await adapter.fetch_trades_from_id(symbol, from_id, 1000)
    detail = {
        "n": len(payload),
        "keys": keys,
        "sample": payload[0] if payload else None,
        "first_id_equals_fromId": bool(df.height and int(df["trade_id"][0]) == from_id),
        "contiguous": bool((df["trade_id"].diff().drop_nulls() == 1).all()),
        "weight_headers": {k: v for k, v in resp.headers.items() if "weight" in k.lower()},
    }
    ok = bool(
        len(payload) == 1000
        and set("apqflTm") <= set(keys)
        and detail["first_id_equals_fromId"]
        and detail["contiguous"]
    )
    return Check("rest_aggTrades", ok, detail)


async def _check_ws(adapter: BinanceUSDM, symbol: str, seconds: float) -> tuple[Check, Any]:
    url = adapter.live_url([symbol], liquidations=True)
    frames: list[dict[str, Any]] = []
    trades: list[pl.DataFrame] = []
    t0 = time.monotonic()
    try:
        async with asyncio.timeout(seconds + 15):
            async for raw in adapter.live_messages(url):
                msg = json.loads(raw)
                if len(frames) < 3 or (msg.get("data", {}).get("e") == "forceOrder"):
                    frames.append(msg)
                b = parse_ws_message(raw)
                if b is not None and b.kind == "trades":
                    trades.append(b.df)
                if time.monotonic() - t0 > seconds:
                    break
    except Exception as exc:
        return Check("ws_stream", False, {"url": url, "error": repr(exc)}), None
    df = pl.concat(trades) if trades else pl.DataFrame()
    detail = {
        "url": url,
        "n_trades": df.height,
        "sample_frames": frames[:5],
        "ids_strictly_increasing": bool(
            df.height and (df["trade_id"].diff().drop_nulls() > 0).all()
        ),
        "ids_contiguous": bool(df.height and (df["trade_id"].diff().drop_nulls() == 1).all()),
    }
    ok = bool(df.height > 0 and detail["ids_strictly_increasing"])
    return Check("ws_stream", ok, detail), df


async def _check_ws_vs_rest(adapter: BinanceUSDM, symbol: str, ws: pl.DataFrame) -> Check:
    if ws is None or ws.height == 0:
        return Check("ws_vs_rest", False, {"error": "no ws trades"})
    first = int(ws["trade_id"][0])
    rest = await adapter.fetch_trades_from_id(symbol, first, 1000)
    j = ws.head(1000).join(rest, on="trade_id", how="inner", suffix="_r")
    same = j.filter(
        (pl.col("price") == pl.col("price_r"))
        & (pl.col("qty") == pl.col("qty_r"))
        & (pl.col("ts_ms") == pl.col("ts_ms_r"))
        & (pl.col("aggressor") == pl.col("aggressor_r"))
    ).height
    return Check(
        "ws_vs_rest",
        j.height > 0 and same == j.height,
        {"compared": j.height, "identical": same},
    )


async def verify_api(adapter: BinanceUSDM, symbols: list[str], tmp: Path) -> list[Check]:
    day = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=2)
    checks: list[Check] = []
    sym = symbols[0]
    checks.append(await _check_vision(adapter, sym, day, tmp, "aggTrades"))
    checks.append(await _check_vision(adapter, sym, day, tmp, "klines"))
    ws_check, ws_df = await _check_ws(adapter, sym, 60.0)
    checks.append(ws_check)
    if ws_df is not None and ws_df.height:
        checks.append(await _check_rest(adapter, sym, int(ws_df["trade_id"][0])))
        checks.append(await _check_ws_vs_rest(adapter, sym, ws_df))
    return checks
