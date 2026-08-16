"""Production composition for provider-backed historical repair services."""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg

from nanodelta.contracts import Market, Provider
from nanodelta.history.engine import BackfillEngine, HistoryJob
from nanodelta.history.postgres import PostgresHistoryState
from nanodelta.history.timeframes import MarketCalendar
from nanodelta.persistence.migrations import Connection
from nanodelta.persistence.postgres import PostgresStore
from nanodelta.pipeline import EtlPipeline
from nanodelta.providers.base import HistoricalClient, ProviderCapability
from nanodelta.providers.dhan import DhanClient
from nanodelta.providers.oanda import OandaClient
from nanodelta.providers.okx import OkxClient
from nanodelta.providers.poloniex import PoloniexClient
from nanodelta.providers.registry import default_provider_registry
from nanodelta.providers.truedata import TrueDataClient


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required when history operations are enabled")
    return value


def _secret(name: str) -> str:
    value = Path(_required(name)).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"{name} points to an empty secret")
    return value


def _symbols(name: str) -> tuple[str, ...]:
    values = tuple(value.strip().upper() for value in _required(name).split(",") if value.strip())
    if not values:
        raise RuntimeError(f"{name} contains no symbols")
    return values


def _timeframes() -> tuple[str, ...]:
    values = tuple(
        value.strip()
        for value in _required("NANODELTA_HISTORY_TIMEFRAMES").split(",")
        if value.strip()
    )
    if not values:
        raise RuntimeError("NANODELTA_HISTORY_TIMEFRAMES contains no timeframes")
    return values


def _dhan_symbols() -> dict[str, str]:
    payload = json.loads(_required("NSE_DHAN_SECURITY_IDS_JSON"))
    if not isinstance(payload, dict) or not payload:
        raise RuntimeError("NSE_DHAN_SECURITY_IDS_JSON must be a non-empty JSON object")
    values = {str(key).strip().upper(): str(value).strip() for key, value in payload.items()}
    if any(not key or not value for key, value in values.items()):
        raise RuntimeError("NSE_DHAN_SECURITY_IDS_JSON cannot contain empty values")
    return values


def build_history_services(
    database_url: str,
) -> tuple[
    dict[Market, BackfillEngine],
    dict[tuple[Market, str, str], HistoryJob],
]:
    """Build provider-backed jobs only for explicitly configured symbols/timeframes."""
    def connect() -> Connection:
        return psycopg.connect(database_url)
    dhan_symbols = _dhan_symbols()
    forex_symbols = _symbols("FOREX_SYMBOLS")
    crypto_symbols = _symbols("CRYPTO_SYMBOLS")
    timeframes = _timeframes()
    clients: dict[Provider, HistoricalClient] = {
        Provider.DHAN: DhanClient(
            client_id=_required("DHAN_CLIENT_ID"),
            access_token=_secret("DHAN_ACCESS_TOKEN_PATH"),
            security_ids=dhan_symbols,
        ),
        Provider.TRUEDATA: TrueDataClient(
            username=_required("TRUEDATA_USERNAME"),
            password=_secret("TRUEDATA_PASSWORD_PATH"),
        ),
        Provider.OANDA: OandaClient(
            account_id=_required("OANDA_ACCOUNT_ID"),
            access_token=_secret("OANDA_ACCESS_TOKEN_PATH"),
            practice=os.environ.get("OANDA_ENVIRONMENT", "practice") == "practice",
        ),
        Provider.OKX: OkxClient(),
        Provider.POLONIEX: PoloniexClient(),
    }
    pipeline = EtlPipeline(PostgresStore(connect))
    state = PostgresHistoryState(connect)
    registry = default_provider_registry()
    engines = {
        market: BackfillEngine(
            pipeline=pipeline,
            registry=registry,
            clients={
                provider: client
                for provider, client in clients.items()
                if client.market is market
            },
            state=state,
            calendars={market: MarketCalendar(market)},
        )
        for market in Market
    }
    jobs: dict[tuple[Market, str, str], HistoryJob] = {}
    market_symbols = {
        Market.NSE: tuple(dhan_symbols),
        Market.FOREX: forex_symbols,
        Market.CRYPTO: crypto_symbols,
    }
    for market, symbols in market_symbols.items():
        route = registry.route(market, ProviderCapability.HISTORICAL_CANDLES)
        for symbol in symbols:
            provider_symbols = {
                provider: symbol.replace("_", "-") if provider is Provider.OKX else symbol
                for provider in route
            }
            for timeframe in timeframes:
                jobs[(market, symbol, timeframe)] = HistoryJob(
                    market, symbol, timeframe, provider_symbols
                )
    return engines, jobs
