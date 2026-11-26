"""Exchange-agnostic venue interface."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

import polars as pl

from flowlevels.stream.types import Batch


class DataNotAvailableError(RuntimeError):
    """The requested historical file does not exist (yet)."""


class ChecksumMismatchError(RuntimeError):
    pass


class RestLookbackExceededError(RuntimeError):
    """The REST trade history no longer covers the requested ids (older than ~48 h)."""


class VenueAdapter(Protocol):
    name: str

    # --- historical -----------------------------------------------------------------
    async def download_trades_day(self, symbol: str, day: dt.date, dest_dir: Path) -> Path:
        """Download + checksum-verify one day of trades; return the local file path."""
        ...

    def read_trades_file(self, path: Path) -> pl.DataFrame:
        """Parse a downloaded trades file into TRADE_SCHEMA, sorted by trade_id."""
        ...

    async def download_klines_1m_day(self, symbol: str, day: dt.date, dest_dir: Path) -> Path: ...

    def read_klines_file(self, path: Path) -> pl.DataFrame: ...

    # --- REST backfill --------------------------------------------------------------
    async def fetch_trades_from_id(self, symbol: str, from_id: int, limit: int) -> pl.DataFrame:
        """Trades with trade_id >= from_id (at most `limit`), TRADE_SCHEMA."""
        ...

    # --- live -------------------------------------------------------------------------
    def live_url(self, symbols: list[str], liquidations: bool) -> str: ...

    def parse_live_message(self, raw: str | bytes) -> Batch | None:
        """Parse one WS frame into a single-event Batch (None for ignored frames)."""
        ...

    def live_messages(self, url: str) -> AsyncIterator[str | bytes]: ...
