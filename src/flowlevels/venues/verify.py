"""`flowlevels verify-api`: check every endpoint/payload assumption against the real API.

Runs on the VPS (needs network). Prints PASS/FAIL per check with the observed values so
docs/DATA_SOURCES.md can be updated with dated evidence. REST usage is kept small (about
ten requests) because the IP weight limit is shared with other services on the host.
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

from flowlevels.footprint.minute import ohlcv_from_trades
from flowlevels.stream.types import MS_MAX, MS_MIN
from flowlevels.venues.base import RestLookbackExceededError
from flowlevels.venues.binance import (
    AGGTRADES_CSV_COLUMNS,
    REST_AGGTRADES_MAX_LIMIT,
    USED_WEIGHT_HEADER,
    BinanceUSDM,
    _has_header,
    aggtrades_daily_path,
    klines_1m_daily_path,
    parse_checksum_file,
    parse_ws_message,
)

LEGACY_WS_ROOT = "wss://fstream.binance.com"


@dataclass
class Check:
    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class VisionData:
    trades: pl.DataFrame | None = None
    klines: pl.DataFrame | None = None


async def _check_vision(
    adapter: BinanceUSDM, symbol: str, day: dt.date, tmp: Path, kind: str, out: VisionData
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
    cs_parts = cs_text.split()
    detail: dict[str, Any] = {
        "url": rel,
        "checksum_file": cs_text.strip()[:200],
        "checksum_parsed": parse_checksum_file(cs_text),
        # sha256sum format: "<64 hex>  <file name>"
        "checksum_is_sha256sum_format": len(cs_parts) == 2
        and len(cs_parts[0]) == 64
        and cs_parts[1] == rel.rsplit("/", 1)[1],
        "zip_members": names,
        "has_header": _has_header(data),
        "first_line": first_line[:200],
        "n_columns": first_line.count(",") + 1,
    }
    if kind == "aggTrades":
        df = adapter.read_trades_file(path)
        ts0 = int(df["ts_ms"][0])
        detail.update(
            rows=df.height,
            ts_first=ts0,
            ts_unit_ms=MS_MIN <= ts0 < MS_MAX,
            ids_contiguous=bool((df["trade_id"].diff().drop_nulls() == 1).all()),
        )
        ok = bool(
            detail["n_columns"] == len(AGGTRADES_CSV_COLUMNS)
            and detail["ts_unit_ms"]
            and detail["checksum_is_sha256sum_format"]
        )
        out.trades = df
    else:
        kl = adapter.read_klines_file(path)
        detail.update(rows=kl.height)
        ok = kl.height == 1440 and bool(detail["checksum_is_sha256sum_format"])
        out.klines = kl
    path.unlink(missing_ok=True)
    return Check(name, ok, detail)


def check_aggressor_semantics(trades: pl.DataFrame, klines: pl.DataFrame) -> Check:
    """`m` semantics: buyer-aggressor volume must equal the kline taker_buy_volume.

    With the mapping m=true -> seller aggressor, our buy volume equals the kline taker-buy
    volume; with the flipped mapping it equals volume - taker_buy_volume. The daily totals
    must match exactly. Per minute, a few percent of minutes differ because an aggTrade
    carries one timestamp `T` for fills spread over up to 100 ms, while klines bucket the
    individual fills: an aggTrade that straddles a minute boundary lands wholly in one
    minute (verified 2026-11-26: every such minute had a multi-fill aggTrade in the last
    100 ms of it or the previous minute). The per-minute threshold allows for that.
    """
    ohlcv = ohlcv_from_trades(trades)
    j = ohlcv.join(
        klines.select(
            pl.col("open_time").alias("minute_ts"),
            pl.col("volume").alias("k_vol"),
            pl.col("taker_buy_volume").alias("k_buy"),
        ),
        on="minute_ts",
        how="inner",
    )
    tol = 1e-6
    lim = tol * j["k_vol"] + 1e-9
    n = max(j.height, 1)
    as_is = int(((j["buy_volume"] - j["k_buy"]).abs() <= lim).sum()) / n
    flipped = int(((j["sell_volume"] - j["k_buy"]).abs() <= lim).sum()) / n
    day_buy = float(j["buy_volume"].sum())
    day_k_buy = float(j["k_buy"].sum())
    day_ok = abs(day_buy - day_k_buy) <= 1e-9 * max(day_k_buy, 1.0)
    detail = {
        "minutes": j.height,
        "match_rate_m_true_is_seller": round(as_is, 6),
        "match_rate_m_true_is_buyer": round(flipped, 6),
        "day_buy_volume": day_buy,
        "day_kline_taker_buy_volume": day_k_buy,
        "day_totals_equal": day_ok,
    }
    ok = j.height > 0 and day_ok and as_is >= 0.8 and flipped < 0.1
    return Check("aggressor_m_semantics", ok, detail)


def _frames_equal(a: pl.DataFrame, b: pl.DataFrame) -> tuple[int, int]:
    j = a.join(b, on="trade_id", how="inner", suffix="_r")
    same = j.filter(
        (pl.col("price") == pl.col("price_r"))
        & (pl.col("qty") == pl.col("qty_r"))
        & (pl.col("ts_ms") == pl.col("ts_ms_r"))
        & (pl.col("aggressor") == pl.col("aggressor_r"))
        & (pl.col("fill_count") == pl.col("fill_count_r"))
    ).height
    return j.height, same


async def _check_vision_vs_rest(
    adapter: BinanceUSDM, symbol: str, trades: pl.DataFrame | None
) -> Check:
    """The archive must equal REST for the same ids (same quantity definition incl. RPI)."""
    if trades is None or trades.height < 1000:
        return Check("vision_vs_rest", False, {"error": "no archive trades"})
    tail = trades.tail(1000)
    try:
        rest = await adapter.fetch_trades_from_id(symbol, int(tail["trade_id"][0]), 1000)
    except Exception as exc:
        return Check("vision_vs_rest", False, {"error": repr(exc)})
    n, same = _frames_equal(tail, rest)
    return Check("vision_vs_rest", n == 1000 and same == n, {"compared": n, "identical": same})


async def _collect_ws(url: str, seconds: float) -> tuple[list[dict[str, Any]], str | None]:
    msgs: list[dict[str, Any]] = []
    t0 = time.monotonic()
    try:
        async with asyncio.timeout(seconds):
            import websockets

            async with websockets.connect(url, ping_interval=None, max_size=2**22) as ws:
                async for raw in ws:
                    msgs.append(json.loads(raw))
                    if time.monotonic() - t0 > seconds:
                        break
    except TimeoutError:
        pass
    except Exception as exc:
        return msgs, repr(exc)
    return msgs, None


async def _check_ws_legacy(symbol: str, seconds: float) -> Check:
    """The unrouted root no longer delivers market streams (decommissioned 2026-04-23)."""
    url = f"{LEGACY_WS_ROOT}/stream?streams={symbol.lower()}@aggTrade"
    msgs, err = await _collect_ws(url, seconds)
    return Check(
        "ws_legacy_unrouted_is_silent",
        not msgs,
        {"url": url, "messages": len(msgs), "error": err, "seconds": seconds},
    )


async def _check_ws(
    adapter: BinanceUSDM, symbol: str, seconds: float
) -> tuple[Check, Check, pl.DataFrame | None]:
    # The live URL (symbol@aggTrade + symbol@forceOrder) plus the all-market liquidation
    # stream, so a forceOrder sample is likely within the window.
    url = adapter.live_url([symbol], liquidations=True) + "/!forceOrder@arr"
    msgs, err = await _collect_ws(url, seconds)
    trades: list[pl.DataFrame] = []
    t_le_e: list[bool] = []
    agg_keys: set[str] = set()
    liq_samples: list[dict[str, Any]] = []
    liq_parsed = 0
    streams: set[str] = set()
    for msg in msgs:
        streams.add(str(msg.get("stream")))
        data = msg.get("data", {})
        if data.get("e") == "aggTrade":
            agg_keys |= set(data)
            t_le_e.append(int(data["T"]) <= int(data["E"]))
        elif data.get("e") == "forceOrder":
            if len(liq_samples) < 3:
                liq_samples.append(msg)
            b = parse_ws_message(json.dumps(msg))
            liq_parsed += int(b is not None and b.kind == "liquidations")
        b = parse_ws_message(json.dumps(msg))
        if b is not None and b.kind == "trades" and b.symbol == symbol:
            trades.append(b.df)
    df = pl.concat(trades) if trades else None
    n_liq = sum(1 for m in msgs if m.get("data", {}).get("e") == "forceOrder")
    detail = {
        "url": url,
        "error": err,
        "frames": len(msgs),
        "streams_seen": sorted(streams),
        "n_trades": 0 if df is None else df.height,
        "aggTrade_keys": sorted(agg_keys),
        "sample_frame": next((m for m in msgs if m.get("data", {}).get("e") == "aggTrade"), None),
        "T_le_E_rate": (sum(t_le_e) / len(t_le_e)) if t_le_e else None,
        "ids_strictly_increasing": bool(
            df is not None and (df["trade_id"].diff().drop_nulls() > 0).all()
        ),
        "ids_contiguous": bool(df is not None and (df["trade_id"].diff().drop_nulls() == 1).all()),
    }
    ok = bool(
        df is not None
        and df.height > 0
        and detail["ids_strictly_increasing"]
        and set("eEsapqflTm") <= agg_keys
        and all(t_le_e)
    )
    liq = Check(
        "ws_forceOrder",
        n_liq > 0 and liq_parsed == n_liq,
        {"n": n_liq, "parsed": liq_parsed, "samples": liq_samples},
    )
    return Check("ws_stream", ok, detail), liq, df


async def _check_rest(adapter: BinanceUSDM, symbol: str, from_id: int) -> Check:
    url = f"{adapter.rest_url}/fapi/v1/aggTrades"
    try:
        r1 = await adapter._http.get(
            url, params={"symbol": symbol, "fromId": from_id, "limit": 1000}
        )
        r2 = await adapter._http.get(
            url, params={"symbol": symbol, "fromId": from_id, "limit": 1000}
        )
        payload = r1.json()
    except Exception as exc:
        return Check("rest_aggTrades", False, {"error": repr(exc)})
    keys = sorted(payload[0].keys()) if payload else []
    df = (await adapter.fetch_trades_from_id(symbol, from_id, 1000)) if payload else None
    w1 = r1.headers.get(USED_WEIGHT_HEADER)
    w2 = r2.headers.get(USED_WEIGHT_HEADER)
    detail = {
        "n": len(payload),
        "keys": keys,
        "sample": payload[0] if payload else None,
        "first_id_equals_fromId": bool(df is not None and int(df["trade_id"][0]) == from_id),
        "contiguous": bool(df is not None and (df["trade_id"].diff().drop_nulls() == 1).all()),
        "used_weight_1m": [w1, w2],
        # Other processes on the IP may add weight between the two calls; >= 20 expected.
        "weight_delta": (int(w2) - int(w1)) if w1 and w2 else None,
    }
    ok = bool(
        r1.status_code == 200
        and len(payload) == 1000
        and set("apqflTm") <= set(keys)
        and detail["first_id_equals_fromId"]
        and detail["contiguous"]
    )
    return Check("rest_aggTrades", ok, detail)


async def _check_rest_limits(
    adapter: BinanceUSDM, symbol: str, recent_id: int, old_id: int | None
) -> Check:
    url = f"{adapter.rest_url}/fapi/v1/aggTrades"
    detail: dict[str, Any] = {}
    r = await adapter._http.get(
        url, params={"symbol": symbol, "fromId": recent_id, "limit": REST_AGGTRADES_MAX_LIMIT + 1}
    )
    detail["limit_1001"] = {"status": r.status_code, "body": r.text[:200]}
    lookback_ok = False
    if old_id is not None:
        try:
            await adapter.fetch_trades_from_id(symbol, old_id, 1)
            detail["fromId_older_than_48h"] = "accepted"
        except RestLookbackExceededError as exc:
            detail["fromId_older_than_48h"] = f"RestLookbackExceededError: {exc}"
            lookback_ok = True
        except Exception as exc:
            detail["fromId_older_than_48h"] = repr(exc)
    info = await adapter._http.get(f"{adapter.rest_url}/fapi/v1/exchangeInfo")
    limits = info.json().get("rateLimits", [])
    detail["rate_limits"] = limits
    weight: dict[str, Any] = next(
        (x for x in limits if x.get("rateLimitType") == "REQUEST_WEIGHT"), {}
    )
    ok = bool(
        r.status_code == 400
        and '"code":-1130' in r.text.replace(" ", "")
        and lookback_ok
        and weight.get("limit") == 2400
        and weight.get("interval") == "MINUTE"
    )
    return Check("rest_limits", ok, detail)


async def _check_ws_vs_rest(adapter: BinanceUSDM, symbol: str, ws: pl.DataFrame | None) -> Check:
    if ws is None or ws.height == 0:
        return Check("ws_vs_rest", False, {"error": "no ws trades"})
    rest = await adapter.fetch_trades_from_id(symbol, int(ws["trade_id"][0]), 1000)
    n, same = _frames_equal(ws.head(1000), rest)
    return Check("ws_vs_rest", n > 0 and same == n, {"compared": n, "identical": same})


async def verify_api(
    adapter: BinanceUSDM, symbols: list[str], tmp: Path, ws_seconds: float = 60.0
) -> list[Check]:
    # Day-2: its archive is published, and its last trades are still inside the 48 h REST
    # window while its first trades are outside it.
    day = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=2)
    checks: list[Check] = []
    sym = symbols[0]
    vision = VisionData()
    checks.append(await _check_vision(adapter, sym, day, tmp, "aggTrades", vision))
    checks.append(await _check_vision(adapter, sym, day, tmp, "klines", vision))
    if vision.trades is not None and vision.klines is not None:
        checks.append(check_aggressor_semantics(vision.trades, vision.klines))
    checks.append(await _check_vision_vs_rest(adapter, sym, vision.trades))
    checks.append(await _check_ws_legacy(sym, 15.0))
    ws_check, liq_check, ws_df = await _check_ws(adapter, sym, ws_seconds)
    checks += [ws_check, liq_check]
    if ws_df is not None and ws_df.height:
        first = int(ws_df["trade_id"][0])
        old = int(vision.trades["trade_id"][0]) if vision.trades is not None else None
        # Start in the recent past so that a full page of 1000 trades exists.
        checks.append(await _check_rest(adapter, sym, first - 5000))
        checks.append(await _check_rest_limits(adapter, sym, first, old))
        checks.append(await _check_ws_vs_rest(adapter, sym, ws_df))
    return checks
