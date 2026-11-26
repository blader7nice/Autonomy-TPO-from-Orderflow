"""CLI levels/footprint/sanity-plot on a SYNTHETIC store (output goes to tmp, not docs/)."""

from pathlib import Path

import pytest

from flowlevels.cli import main
from flowlevels.config import load_config
from flowlevels.data.storage import ParquetStore
from flowlevels.footprint.minute import MinuteBuilder
from flowlevels.levels.report import levels_at, pick_days, sanity_plot
from tests.helpers import T0, synthetic_trades

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "configs/default.yaml")
DAY = 86_400_000


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> ParquetStore:
    root = tmp_path_factory.mktemp("data")
    st = ParquetStore(root)
    tape = synthetic_trades(30_000, seed=3, start_ms=T0, span_ms=5 * DAY)
    b = MinuteBuilder(0.1, 10.0)
    a, z = b.push(tape), b.flush()
    import polars as pl

    oh, fp = pl.concat([a.ohlcv, z.ohlcv]), pl.concat([a.footprint, z.footprint])
    st.write_split_by_day("ohlcv_1m", "BTCUSDT", "hist", oh, "minute_ts")
    st.write_split_by_day("footprint_1m", "BTCUSDT", "hist", fp, "minute_ts")
    # Days 0-3 are complete archive days; day 4 plays today's partial live day.
    days = st.days("ohlcv_1m", "BTCUSDT")
    for d in days[:-1]:
        holes = [{"after_ms": T0 + 3_600_000, "before_ms": T0 + 5_400_000, "seconds": 1800.0}]
        st.mark_complete("ohlcv_1m", "BTCUSDT", d, {"holes": holes if d == days[0] else []})
    return st


def test_levels_at_reads_store(store: ParquetStore) -> None:
    ls = levels_at(store, CFG, "BTCUSDT", T0 + 3 * DAY + 3_600_000)
    assert ls.level("PDH") is not None and ls.zones


def test_cli_levels_and_footprint(
    store: ParquetStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("FLOWLEVELS_DATA_DIR", str(store.data_dir))
    assert main(["levels", "--symbol", "BTCUSDT", "--ts", "2026-01-03T05:00"]) == 0
    out = capsys.readouterr().out
    assert "VAL d" in out and "Confluence zones" in out
    assert (
        main(["footprint", "--symbol", "BTCUSDT", "--ts", "2026-01-03T05:07", "--res", "5m"]) == 0
    )
    out = capsys.readouterr().out
    assert "5m bar 2026-01-03 05:05 UTC" in out and "<POC" in out


def test_sanity_plot(store: ParquetStore, tmp_path: Path) -> None:
    days = pick_days(store, "BTCUSDT", 10)
    all_days = store.days("ohlcv_1m", "BTCUSDT")
    # Complete days with a prior day: days 1-3. Day 0 has no prior day; day 4 is partial.
    assert days == all_days[1:-1]
    p = sanity_plot(store, CFG, "BTCUSDT", days[0], tmp_path)
    assert p.exists() and p.stat().st_size > 10_000


def test_sanity_plot_with_hole(store: ParquetStore, tmp_path: Path) -> None:
    day0 = store.days("ohlcv_1m", "BTCUSDT")[0]
    p = sanity_plot(store, CFG, "BTCUSDT", day0, tmp_path)
    assert p.exists() and p.stat().st_size > 10_000
