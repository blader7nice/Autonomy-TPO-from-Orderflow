"""Independent cross-check (flowlevels.paper.verify) and the chart readout.

These tests check that:
- the reference math agrees with the rule code on random data;
- the cross-check passes on a ledger written by the real engine;
- it fails on every kind of tampering;
- the REST tail behaves like the engine (newest row dropped).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest
from fastapi.testclient import TestClient

from flowlevels.levels.profile import tpo_profile, value_area
from flowlevels.paper import verify
from flowlevels.paper.store import Store
from flowlevels.strategies import method4 as m4
from flowlevels.strategies import method5 as m5
from flowlevels.web import readout
from flowlevels.web.app import create_app
from tests.test_paper import CFG0, DAY, MIN, T0, FakeExchange, _cfg, _engine, _klines, _setup


def _sync(ex: FakeExchange) -> verify.FetchSync:
    def fetch(symbol: str, start: int | None, limit: int) -> pl.DataFrame:
        return asyncio.run(ex.fetch(symbol, start, limit))

    return fetch


def _ledger(tmp: Path) -> tuple[Any, pl.DataFrame, FakeExchange]:
    """A ledger written by the real engine (synthetic candles), live store flushed."""
    cfg = _cfg()
    full, ex = _setup(tmp)
    eng = _engine(tmp, ex, cfg)
    asyncio.run(eng.start())
    for _ in range(8):
        ex.now += 6 * 60 * MIN
        asyncio.run(eng.step())
    asyncio.run(eng.shutdown())
    eng.store.close()
    return cfg, full, ex


def _run(tmp: Path, cfg: Any, fetch: verify.FetchSync | None) -> dict[str, Any]:
    store = Store(tmp / "paper" / "paper.sqlite", readonly=True)
    try:
        return verify.run(cfg, store, verify.Candles(tmp, fetch), ["BTCUSDT"], rest=True)
    finally:
        store.close()


def _checks(res: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in res["symbols"][0]["checks"]}


# ----------------------------------------------------------------------- reference math


def test_reference_math_matches_rule_code() -> None:
    df = _klines(20, seed=5)
    x = df["close"].to_numpy()
    e_ref = np.array(verify.ema_ref(x.tolist(), 50))
    assert np.max(np.abs(e_ref - m5.ema(x, 50)) / x) < 1e-12
    ts, o, h, lo, c = (df[k].to_numpy() for k in ("minute_ts", "open", "high", "low", "close"))
    for step in (m4.M15, m4.M30, m4.H1, m4.H4, m4.DAY):
        b = m4.make_bars(df, step)
        ref = verify.agg_bars(ts, o, h, lo, c, step)
        assert list(ref) == b.ts.tolist()
        assert [v[1] for v in ref.values()] == b.h.tolist()
        assert [v[3] for v in ref.values()] == b.c.tolist()
    rng = np.random.default_rng(3)
    for sym in ("BTCUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"):
        sc = CFG0.symbol(sym)
        scale = {"BTCUSDT": 1.0, "SOLUSDT": 1 / 600, "XRPUSDT": 1 / 40_000, "DOGEUSDT": 1 / 400_000}
        for _ in range(6):
            a = int(rng.integers(0, len(ts) - 1440))
            w = df[a : a + 1440].with_columns(
                *(pl.col(k) * scale[sym] for k in ("open", "high", "low", "close"))
            )
            va = value_area(
                tpo_profile(w, 30, sc.tpo_tick, sc.exchange_tick), CFG0.levels.value_area_pct
            )
            assert va is not None
            got = verify.tpo_value_area(
                w["minute_ts"].to_numpy(),
                w["high"].to_numpy(),
                w["low"].to_numpy(),
                30 * MIN,
                sc.tpo_tick,
                sc.exchange_tick,
                CFG0.levels.value_area_pct,
            )
            assert got == (va.vah, va.poc, va.val)


def test_tpo_ties_follow_the_definition() -> None:
    # flat profile: every row has the same count -> POC is the middle row (lower on a tie)
    n = 48
    ts = T0 + np.arange(n) * 30 * MIN
    h, lo = np.full(n, 104.0), np.full(n, 100.0)
    got = verify.tpo_value_area(ts, h, lo, 30 * MIN, 1.0, 0.1, 0.7)
    va = value_area(
        tpo_profile(pl.DataFrame({"minute_ts": ts, "high": h, "low": lo}), 30, 1.0, 0.1), 0.7
    )
    assert va is not None and got == (va.vah, va.poc, va.val)
    assert got[1] == 102.5  # rows 100..104 -> middle row 102


# ----------------------------------------------------------------------- end to end


def test_cross_check_passes_on_engine_ledger(tmp_path: Path) -> None:
    cfg, _, ex = _ledger(tmp_path)
    res = _run(tmp_path, cfg, _sync(ex))
    assert res["ok"] is True, res
    ch = _checks(res)
    assert set(ch) == {
        "candles_vs_rest",
        "bars",
        "ema50_4h",
        "ema50_1d",
        "tpo_now",
        "tpo_history",
        "bias",
    }
    assert all(c["ok"] is True and c["n"] > 0 for c in ch.values()), ch
    assert ch["candles_vs_rest"]["n"] == 60 and ch["tpo_now"]["n"] == 6


@pytest.mark.parametrize(
    ("what", "check"),
    [
        ("ema4", "ema50_4h"),
        ("emad", "ema50_1d"),
        ("bar", "bars"),
        ("level", "tpo_now"),
        ("bias", "bias"),
        ("rest", "candles_vs_rest"),
    ],
)
def test_cross_check_catches_tampering(tmp_path: Path, what: str, check: str) -> None:
    cfg, _, ex = _ledger(tmp_path)
    db = Store(tmp_path / "paper" / "paper.sqlite")
    rows = {
        r["tf"]: json.loads(r["payload"])
        for r in db.rows("SELECT tf, payload FROM chart_cache WHERE symbol='BTCUSDT'")
    }
    if what in ("ema4", "emad"):
        rows["30m"][what][-1][1] *= 1.0001
    elif what == "bar":
        rows["1h"]["bars"][-3][2] += 0.1
    elif what == "level":
        rows["overlay"]["levels"][1]["price"] += 10.0
    elif what == "bias":
        rows["overlay"]["bias"]["dir"] = 1 - abs(int(rows["overlay"]["bias"]["dir"]))
    with db.tx() as con:
        for tf, p in rows.items():
            con.execute(
                "UPDATE chart_cache SET payload=? WHERE symbol='BTCUSDT' AND tf=?",
                (json.dumps(p), tf),
            )
    db.close()
    fetch = _sync(ex)
    if what == "rest":

        def fetch(symbol: str, start: int | None, limit: int) -> pl.DataFrame:
            df = asyncio.run(ex.fetch(symbol, start, limit))
            return df.with_columns(
                pl.when(pl.int_range(pl.len()) == 5)
                .then(pl.col("close") + 1.0)
                .otherwise(pl.col("close"))
                .alias("close")
            )

    res = _run(tmp_path, cfg, fetch)
    ch = _checks(res)
    assert res["ok"] is False and ch[check]["ok"] is False, ch
    assert ch[check]["detail"]
    others = [n for n, c in ch.items() if n != check and c["ok"] is False]
    assert not others, others


def test_candles_tail_drops_newest_row_and_caches(tmp_path: Path) -> None:
    _, ex = _setup(tmp_path, hist_days=3, total_days=4)
    ex.now = T0 + 3 * DAY + 10 * MIN  # forming minute = 10
    calls: list[int | None] = []

    def fetch(symbol: str, start: int | None, limit: int) -> pl.DataFrame:
        calls.append(start)
        return asyncio.run(ex.fetch(symbol, start, limit))

    cs = verify.Candles(tmp_path, fetch, tail_s=60.0)
    df, disk_last = cs.get("BTCUSDT", T0)
    assert disk_last == T0 + 3 * DAY - MIN
    assert int(df["minute_ts"][-1]) == T0 + 3 * DAY + 9 * MIN  # forming minute 10 dropped
    assert df["minute_ts"].is_sorted() and df["minute_ts"].n_unique() == df.height
    cs.get("BTCUSDT", T0)
    assert len(calls) == 1  # cached
    cs.get("BTCUSDT", T0, fresh=True)
    assert len(calls) == 2

    def broken(symbol: str, start: int | None, limit: int) -> pl.DataFrame:
        raise OSError("network down")

    cs.fetch = broken
    df2, _ = cs.get("BTCUSDT", T0, fresh=True)
    assert df2.height == df.height and "OSError" in cs.tail_error["BTCUSDT"]


# ----------------------------------------------------------------------- readout + web


def test_readout_values(tmp_path: Path) -> None:
    df = _klines(10, seed=9)
    d0 = T0 + 8 * DAY
    lv = readout.rule_levels(CFG0, "BTCUSDT", df, [d0])
    ref = verify.ref_levels(df, d0, CFG0, "BTCUSDT")
    assert set(lv[d0]) == {"pdVAH", "pdPOC", "pdVAL", "pwVAH", "pwPOC", "pwVAL"} == set(ref)
    assert all(lv[d0][k] == ref[k] for k in ref)
    t = d0 + 90 * MIN  # 01:30: session VWAP of 00:00 .. 01:29
    w = df.filter((pl.col("minute_ts") >= d0) & (pl.col("minute_ts") < t))
    tp = (w["high"] + w["low"] + w["close"]) / 3
    want = float((tp * w["volume"]).sum() / w["volume"].sum())
    got = readout.session_vwap(df, [t, d0 + MIN, d0])
    assert got[0] == pytest.approx(want, rel=1e-12)
    r0 = df.filter(pl.col("minute_ts") == d0).row(0, named=True)
    assert got[1] == pytest.approx((r0["high"] + r0["low"] + r0["close"]) / 3, rel=1e-12)
    assert got[2] is not None  # 00:00 exactly = the previous session's final VWAP


def test_web_readout_and_verify_endpoints(tmp_path: Path) -> None:
    cfg, _, _ = _ledger(tmp_path)
    c = TestClient(create_app(cfg, tmp_path / "paper" / "paper.sqlite", tmp_path,
                              verify_loop=False))  # fmt: skip
    ch = c.get("/api/chart/BTCUSDT/1h").json()
    rd = ch["readout"]
    assert len(rd["vwap"]) == len(ch["bars"]) and rd["levels"]
    day = max(rd["levels"])
    assert rd["levels"][day][1] is not None  # pdPOC
    assert c.get("/api/verify").json()["result"] is None
    res = c.post("/api/verify/run").json()["result"]
    assert res["ok"] is True and res["rest"] is False
    names = {x["name"] for x in res["symbols"][0]["checks"]}
    assert "candles_vs_rest" not in names and "bias" in names
    assert c.get("/api/health").json()["verify"]["result"]["ok"] is True
