"""Data-hole exclusions (D-031)."""

import datetime as dt

import polars as pl

from flowlevels.config import LevelsConfig
from flowlevels.data.quality import Hole, bar_mask, level_session_window, overlaps_any
from flowlevels.data.storage import day_start_ms

# The real archive hole (DATA_SOURCES.md): 2026-10-27 00:02:46 - 00:30:05 UTC.
D = day_start_ms(dt.date(2026, 10, 27))
HOLE = Hole("BTCUSDT", D + 166_000, D + 1_805_000)


def test_hole_overlap_is_open_interval() -> None:
    assert HOLE.overlaps(D, D + 60_000 * 3)  # minute 00:02 contains the hole start
    assert not HOLE.overlaps(D, D + 166_000)  # ends exactly at the last trade before it
    assert not HOLE.overlaps(D + 1_805_000, D + 2_000_000)  # starts at the first trade after
    assert overlaps_any([HOLE], D + 1_000_000, D + 1_000_001)


def test_bar_mask_5m() -> None:
    bars = pl.Series([D - 300_000, D, D + 300_000 * 5, D + 300_000 * 6, D + 300_000 * 7])
    # 23:55 ok; 00:00 has the hole start; 00:25 is inside; [00:30, 00:35) still contains
    # 00:30:00-00:30:05 of the hole; 00:35 is clean.
    assert bar_mask(bars, 300_000, [HOLE]).to_list() == [False, True, True, True, False]


def test_level_session_window_covers_month_week_and_npocs() -> None:
    lc = LevelsConfig()
    # 2026-11-05 (Thursday): prior week is 10-26 .. 11-01 -> includes the 10-27 hole.
    s, e = level_session_window(dt.date(2026, 11, 5), lc)
    assert s <= HOLE.after_ms and HOLE.overlaps(s, e)
    # 2026-11-09 (Monday): prior week 11-02 .. 11-08, month from 11-01, nPOCs from 10-30.
    s, e = level_session_window(dt.date(2026, 11, 9), lc)
    assert not HOLE.overlaps(s, e)
