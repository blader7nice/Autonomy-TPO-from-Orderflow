"""Dashboard API on a ledger written by the real engine (synthetic data, no network)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from flowlevels.web.app import create_app
from tests.test_paper import MIN, _cfg, _engine, _setup


def _client(tmp: Path) -> TestClient:
    cfg = _cfg()
    _, ex = _setup(tmp)
    eng = _engine(tmp, ex, cfg)
    asyncio.run(eng.start())
    for _ in range(12):
        ex.now += 6 * 60 * MIN
        asyncio.run(eng.step())
    eng.store.close()
    return TestClient(create_app(cfg, tmp / "paper" / "paper.sqlite", tmp))


def test_api_endpoints(tmp_path: Path) -> None:
    c = _client(tmp_path)
    s = c.get("/api/summary").json()
    assert s["demo"] is False and [v["id"] for v in s["variants"]] == ["a", "b", "c", "d"]
    tr = c.get("/api/trades").json()["trades"]
    assert tr, "synthetic data should give trades"
    one = c.get(f"/api/trades/{tr[0]['id']}").json()
    assert one["chart"]["bars"] and one["reasons"]
    assert {r["rule"] for r in one["reasons"]} >= {"Trigger", "Stop"}
    wins = c.get("/api/trades", params={"result": "win"}).json()["trades"]
    assert all(t["status"] == "closed" and t["net_r"] > 0 for t in wins)
    only_a = c.get("/api/trades", params={"variant": "a"}).json()["trades"]
    assert {t["variant"] for t in only_a} <= {"a"}
    perf = c.get("/api/performance").json()
    assert len(perf["ledgers"]) == 4
    assert all("progress" in x["stats"] and "backtest" in x for x in perf["ledgers"])
    for tf in ("15m", "30m", "1h", "4h"):
        ch = c.get(f"/api/chart/BTCUSDT/{tf}").json()
        assert ch["bars"] and "overlay" in ch and "markers" in ch
    assert c.get("/api/chart/BTCUSDT/2h").status_code == 404
    assert c.get("/api/chart/FOOUSDT/1h").status_code == 404
    h = c.get("/api/health").json()
    assert h["engine"]["status"] in ("running", "stale", "stopped")
    assert h["symbols"][0]["symbol"] == "BTCUSDT"
    assert c.get("/api/positions").status_code == 200
    assert c.get("/").status_code == 200 and "app.js" in c.get("/").text
    with c.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert "dv" in msg and "server_ms" in msg


def test_png_path_must_stay_inside_data_dir(tmp_path: Path) -> None:
    c = _client(tmp_path)
    import sqlite3

    db = sqlite3.connect(tmp_path / "paper" / "paper.sqlite")
    db.execute("UPDATE trades SET chart_png='/etc/passwd'")
    db.commit()
    tid = c.get("/api/trades").json()["trades"][0]["id"]
    assert c.get(f"/api/trades/{tid}/png").status_code == 404


def test_no_database_gives_503(tmp_path: Path) -> None:
    c = TestClient(create_app(_cfg(), tmp_path / "missing.sqlite", tmp_path))
    assert c.get("/api/trades").status_code == 503
    assert c.get("/").status_code == 200
