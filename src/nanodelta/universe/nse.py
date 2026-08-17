"""NSE symbols.csv loading, Dhan security resolution, and history-job creation."""

from __future__ import annotations

import asyncio
import csv
import io
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

import httpx

from nanodelta.contracts import Market, Provider, utc
from nanodelta.history import BackfillEngine, HistoryJob, HistoryRun
from nanodelta.providers.dhan import DhanClient
from nanodelta.providers.dhan_auth import (
    DhanAccessToken,
    DhanSecretFiles,
    DhanTokenProvider,
    StaticDhanTokenProvider,
)


@dataclass(frozen=True)
class NseSymbolSpec:
    symbol: str
    security_id: str | None
    exchange_segment: str
    instrument: str
    timeframes: tuple[str, ...]
    trade_horizon: str


@dataclass(frozen=True)
class DhanInstrument:
    symbol: str
    security_id: str
    exchange_segment: str
    instrument: str


class TextTransport(Protocol):
    async def get_text(self, url: str) -> str: ...


class DhanTokenSource(Protocol):
    client_id: str

    async def token(self, *, now: datetime) -> DhanAccessToken: ...


class HttpxTextTransport:
    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self._timeout = timeout_seconds

    async def get_text(self, url: str) -> str:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text


class DhanInstrumentMaster:
    """Resolve canonical symbols against Dhan's official detailed security master."""

    URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

    def __init__(self, transport: TextTransport | None = None) -> None:
        self._transport = transport or HttpxTextTransport()

    async def resolve(self, symbols: tuple[str, ...]) -> dict[str, DhanInstrument]:
        requested = {self._symbol(symbol): symbol for symbol in symbols}
        text = await self._transport.get_text(self.URL)
        matches: dict[str, list[DhanInstrument]] = {key: [] for key in requested}
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        if reader.fieldnames is None:
            raise ValueError("Dhan instrument master has no header")
        for row in reader:
            if not self._is_nse_equity(row):
                continue
            security_id = self._value(row, "SECURITY_ID", "SEM_SMST_SECURITY_ID")
            if not security_id:
                continue
            aliases = {
                self._symbol(value)
                for value in (
                    self._value(row, "SYMBOL_NAME", "SM_SYMBOL_NAME"),
                    self._value(row, "TRADING_SYMBOL", "SEM_TRADING_SYMBOL"),
                    self._value(row, "DISPLAY_NAME", "SEM_CUSTOM_SYMBOL"),
                    self._value(row, "UNDERLYING_SYMBOL"),
                )
                if value
            }
            for alias in aliases & requested.keys():
                matches[alias].append(
                    DhanInstrument(
                        self._symbol(requested[alias]),
                        security_id,
                        "NSE_EQ",
                        "EQUITY",
                    )
                )
        resolved: dict[str, DhanInstrument] = {}
        for normalized, original in requested.items():
            unique = {item.security_id: item for item in matches[normalized]}
            if not unique:
                raise LookupError(f"NSE symbol not found in Dhan instrument master: {original}")
            if len(unique) > 1:
                raise ValueError(f"NSE symbol is ambiguous in Dhan instrument master: {original}")
            resolved[self._symbol(original)] = next(iter(unique.values()))
        return resolved

    @classmethod
    def _is_nse_equity(cls, row: Mapping[str, str]) -> bool:
        exchange = cls._value(row, "EXCH_ID", "SEM_EXM_EXCH_ID").upper()
        segment = cls._value(row, "SEGMENT", "SEM_SEGMENT").upper()
        instrument = cls._value(
            row, "INSTRUMENT", "SEM_INSTRUMENT_NAME", "INSTRUMENT_TYPE", "SEM_EXCH_INSTRUMENT_TYPE"
        ).upper()
        return (
            exchange == "NSE"
            and segment in {"E", "EQ", "NSE_EQ"}
            and (not instrument or "EQUITY" in instrument)
        )

    @staticmethod
    def _value(row: Mapping[str, str], *names: str) -> str:
        for name in names:
            value = row.get(name)
            if value is not None and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _symbol(value: str) -> str:
        return value.strip().upper().removesuffix("-EQ")


@dataclass(frozen=True)
class DhanUniverse:
    instruments: tuple[DhanInstrument, ...]
    jobs: tuple[HistoryJob, ...]
    client: DhanClient

    async def sync_all(
        self,
        engine: BackfillEngine,
        *,
        now: datetime,
        concurrency: int = 4,
    ) -> tuple[HistoryRun, ...]:
        if concurrency < 1:
            raise ValueError("history concurrency must be positive")
        semaphore = asyncio.Semaphore(concurrency)

        async def sync(job: HistoryJob) -> HistoryRun:
            async with semaphore:
                return await engine.sync(job, now=now)

        return tuple(await asyncio.gather(*(sync(job) for job in self.jobs)))


class DhanNseUniverseBuilder:
    def __init__(
        self,
        *,
        token_provider: DhanTokenSource,
        instrument_master: DhanInstrumentMaster | None = None,
    ) -> None:
        self._tokens = token_provider
        self._master = instrument_master or DhanInstrumentMaster()

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        instrument_master: DhanInstrumentMaster | None = None,
    ) -> tuple[DhanNseUniverseBuilder, Path]:
        values = environ or os.environ
        client_id = values.get("DHAN_CLIENT_ID", "").strip()
        csv_value = values.get("NSE_SYMBOLS_CSV", "").strip()
        if not client_id or not csv_value:
            raise ValueError("DHAN_CLIENT_ID and NSE_SYMBOLS_CSV are required")
        access_token = values.get("DHAN_ACCESS_TOKEN", "").strip()
        if access_token:
            tokens: DhanTokenSource = StaticDhanTokenProvider(
                client_id=client_id, access_token=access_token
            )
        else:
            pin_path = values.get("DHAN_PIN_PATH", "").strip()
            totp_path = values.get("DHAN_TOTP_SECRET_PATH", "").strip()
            if not pin_path or not totp_path:
                raise ValueError(
                    "set DHAN_ACCESS_TOKEN or both DHAN_PIN_PATH and DHAN_TOTP_SECRET_PATH"
                )
            tokens = DhanTokenProvider(
                client_id=client_id,
                secrets=DhanSecretFiles(Path(pin_path), Path(totp_path)),
            )
        return cls(token_provider=tokens, instrument_master=instrument_master), Path(csv_value)

    async def build(self, csv_path: Path, *, now: datetime) -> DhanUniverse:
        now = utc(now, "now")
        specs = load_nse_symbols(csv_path)
        unresolved = tuple(spec.symbol for spec in specs if spec.security_id is None)
        resolved = await self._master.resolve(unresolved) if unresolved else {}
        instruments = tuple(
            DhanInstrument(
                spec.symbol,
                spec.security_id or resolved[spec.symbol].security_id,
                spec.exchange_segment,
                spec.instrument,
            )
            for spec in specs
        )
        token = await self._tokens.token(now=now)
        security_ids = {item.symbol: item.security_id for item in instruments}
        client = DhanClient(
            client_id=self._tokens.client_id,
            access_token=token.value,
            security_ids=security_ids,
        )
        jobs = tuple(
            HistoryJob(
                Market.NSE,
                spec.symbol,
                timeframe,
                {Provider.DHAN: spec.symbol},
                target_days=730,
            )
            for spec in specs
            for timeframe in spec.timeframes
        )
        return DhanUniverse(instruments, jobs, client)


def load_nse_symbols(path: Path) -> tuple[NseSymbolSpec, ...]:
    if not path.is_file():
        raise FileNotFoundError(f"NSE symbols CSV does not exist: {path}")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or "symbol" not in {
            name.strip().lower() for name in reader.fieldnames
        }:
            raise ValueError("symbols.csv requires a symbol column")
        rows = list(reader)
    result = []
    seen: set[str] = set()
    for line, raw in enumerate(rows, start=2):
        row = {str(name).strip().lower(): (value or "").strip() for name, value in raw.items()}
        enabled = row.get("enabled", "true").lower()
        if enabled in {"false", "0", "no", "n"}:
            continue
        if enabled not in {"true", "1", "yes", "y", ""}:
            raise ValueError(f"invalid enabled value on symbols.csv line {line}")
        symbol = row.get("symbol", "").upper().removesuffix("-EQ")
        if not symbol:
            raise ValueError(f"empty symbol on symbols.csv line {line}")
        if symbol in seen:
            raise ValueError(f"duplicate NSE symbol in symbols.csv: {symbol}")
        seen.add(symbol)
        raw_timeframes = row.get("timeframes", "5m|15m|1h|1d")
        timeframes = tuple(
            value.strip().lower()
            for value in raw_timeframes.replace(";", "|").split("|")
            if value.strip()
        )
        if not timeframes:
            raise ValueError(f"no timeframes configured for {symbol}")
        unsupported = set(timeframes) - DhanClient.SUPPORTED_HISTORY_TIMEFRAMES
        if unsupported:
            raise ValueError(
                f"Dhan direct history does not support {sorted(unsupported)} for {symbol}"
            )
        result.append(
            NseSymbolSpec(
                symbol,
                row.get("security_id") or None,
                row.get("exchange_segment") or "NSE_EQ",
                row.get("instrument") or "EQUITY",
                timeframes,
                row.get("trade_horizon") or "intraday",
            )
        )
    if not result:
        raise ValueError("symbols.csv contains no enabled NSE symbols")
    return tuple(result)
