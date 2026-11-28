"""Partitioned parquet storage: <root>/<dataset>/symbol=S/date=YYYY-MM-DD/<part>.parquet.

Writes are atomic (temp file + rename) and idempotent (same part name overwrites).
A `_SUCCESS.json` marker in a date partition means the day is complete.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import polars as pl

DATASETS = {
    "raw_aggtrades": "raw/aggtrades",
    "raw_liquidations": "raw/liquidations",
    "footprint_1m": "derived/footprint_1m",
    "ohlcv_1m": "derived/ohlcv_1m",
}


def utc_date(ts_ms: int) -> dt.date:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.UTC).date()


def day_start_ms(day: dt.date) -> int:
    return int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC).timestamp() * 1000)


class ParquetStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)

    def partition(self, dataset: str, symbol: str, day: dt.date) -> Path:
        return self.data_dir / DATASETS[dataset] / f"symbol={symbol}" / f"date={day.isoformat()}"

    def write_part(
        self, dataset: str, symbol: str, day: dt.date, part: str, df: pl.DataFrame
    ) -> Path:
        d = self.partition(dataset, symbol, day)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{part}.parquet"
        tmp = d / f".{part}.parquet.tmp"
        df.write_parquet(tmp, compression="zstd", compression_level=3, statistics=True)
        os.replace(tmp, path)
        return path

    def write_split_by_day(
        self, dataset: str, symbol: str, part: str, df: pl.DataFrame, ts_col: str
    ) -> list[Path]:
        """Write df into date partitions according to ts_col (UTC day)."""
        if df.height == 0:
            return []
        paths = []
        days = df.select((pl.col(ts_col) // 86_400_000).alias("d"))["d"].unique().sort()
        for d in days.to_list():
            sub = df.filter(pl.col(ts_col) // 86_400_000 == d)
            day = dt.date(1970, 1, 1) + dt.timedelta(days=int(d))
            paths.append(self.write_part(dataset, symbol, day, part, sub))
        return paths

    def remove_parts(self, dataset: str, symbol: str, day: dt.date, keep: str) -> list[str]:
        """Delete every parquet part of a day partition except `keep`; returns removed names."""
        d = self.partition(dataset, symbol, day)
        removed = []
        if d.exists():
            for f in sorted(d.glob("*.parquet")):
                if f.stem != keep:
                    f.unlink()
                    removed.append(f.name)
        return removed

    def mark_complete(self, dataset: str, symbol: str, day: dt.date, info: dict[str, Any]) -> None:
        d = self.partition(dataset, symbol, day)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "._SUCCESS.json.tmp"
        tmp.write_text(json.dumps(info, indent=2, sort_keys=True))
        os.replace(tmp, d / "_SUCCESS.json")

    def completion(self, dataset: str, symbol: str, day: dt.date) -> dict[str, Any] | None:
        p = self.partition(dataset, symbol, day) / "_SUCCESS.json"
        if not p.exists():
            return None
        info: dict[str, Any] = json.loads(p.read_text())
        return info

    def days(self, dataset: str, symbol: str) -> list[dt.date]:
        root = self.data_dir / DATASETS[dataset] / f"symbol={symbol}"
        if not root.exists():
            return []
        out = []
        for d in root.iterdir():
            if d.is_dir() and d.name.startswith("date="):
                out.append(dt.date.fromisoformat(d.name[5:]))
        return sorted(out)

    def files(self, dataset: str, symbol: str, start: dt.date, end: dt.date) -> Iterator[Path]:
        """Parquet files for days in [start, end] (inclusive), in day/part order."""
        for day in self.days(dataset, symbol):
            if start <= day <= end:
                yield from sorted(self.partition(dataset, symbol, day).glob("*.parquet"))

    def read(self, dataset: str, symbol: str, start: dt.date, end: dt.date) -> pl.DataFrame:
        files = list(self.files(dataset, symbol, start, end))
        if not files:
            raise FileNotFoundError(f"No {dataset} data for {symbol} {start}..{end}")
        return pl.concat([pl.read_parquet(f) for f in files], how="vertical")

    def delete_day(self, dataset: str, symbol: str, day: dt.date) -> None:
        d = self.partition(dataset, symbol, day)
        if d.exists():
            for f in d.iterdir():
                f.unlink()
            d.rmdir()


def dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
