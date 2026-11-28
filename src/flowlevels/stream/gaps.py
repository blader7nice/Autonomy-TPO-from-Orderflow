"""Trade-ID gap detection (aggTrade ids are sequential per symbol)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import polars as pl


@dataclass(frozen=True, slots=True)
class Gap:
    symbol: str
    after_id: int  # last id seen before the gap
    before_id: int  # first id seen after the gap
    after_ts_ms: int
    before_ts_ms: int
    resolved: bool = False
    resolution: str = ""

    @property
    def missing(self) -> int:
        return self.before_id - self.after_id - 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["missing"] = self.missing
        return d


@dataclass
class GapTracker:
    """Observes ordered trade batches for one symbol and records id gaps."""

    symbol: str
    last_id: int | None = None
    last_ts_ms: int | None = None
    gaps: list[Gap] = field(default_factory=list)
    duplicates_dropped: int = 0

    def find_gaps(self, trades: pl.DataFrame) -> list[Gap]:
        """Gaps that `trades` would introduce (does not change state)."""
        if trades.height == 0:
            return []
        ids = trades["trade_id"]
        ts = trades["ts_ms"]
        out: list[Gap] = []
        if self.last_id is not None and int(ids[0]) > self.last_id + 1:
            assert self.last_ts_ms is not None
            out.append(Gap(self.symbol, self.last_id, int(ids[0]), self.last_ts_ms, int(ts[0])))
        if trades.height > 1:
            diffs = ids.diff().fill_null(1)
            for i in (diffs > 1).arg_true().to_list():
                out.append(
                    Gap(self.symbol, int(ids[i - 1]), int(ids[i]), int(ts[i - 1]), int(ts[i]))
                )
        return out

    def drop_seen(self, trades: pl.DataFrame) -> pl.DataFrame:
        """Remove trades with id <= last_id (replays after reconnect / overlapping pages)."""
        if self.last_id is None or trades.height == 0:
            return trades
        fresh = trades.filter(pl.col("trade_id") > self.last_id)
        self.duplicates_dropped += trades.height - fresh.height
        return fresh

    def observe(self, trades: pl.DataFrame, resolution: str = "") -> list[Gap]:
        """Record gaps in `trades` (already de-duplicated) and advance state."""
        gaps = self.find_gaps(trades)
        self.gaps.extend(gaps)
        if trades.height:
            self.last_id = int(trades["trade_id"][-1])
            self.last_ts_ms = int(trades["ts_ms"][-1])
        return gaps

    def record_resolved(self, gap: Gap, resolution: str) -> None:
        self.gaps.append(
            Gap(
                gap.symbol,
                gap.after_id,
                gap.before_id,
                gap.after_ts_ms,
                gap.before_ts_ms,
                resolved=True,
                resolution=resolution,
            )
        )
