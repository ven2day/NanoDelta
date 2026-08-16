"""Environment composition for realtime, supervised, paper-only market cycles."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import psycopg

from nanodelta.contracts import Market, Provider
from nanodelta.decisions_postgres import PostgresDecisionLedger
from nanodelta.paper import ExecutionPolicy, PostgresPaperExecutionEngine
from nanodelta.persistence.postgres import PostgresStore
from nanodelta.pipeline import EtlPipeline
from nanodelta.providers.dhan import DhanClient
from nanodelta.providers.oanda import OandaClient
from nanodelta.providers.okx import OkxClient
from nanodelta.providers.poloniex import PoloniexClient
from nanodelta.providers.registry import default_provider_registry
from nanodelta.providers.truedata import TrueDataClient
from nanodelta.risk import RiskEngine
from nanodelta.runtime.paper_decision import PaperDecisionService
from nanodelta.runtime.paper_policy import build_allocation_policy, build_risk_limits
from nanodelta.runtime.realtime import RealtimeMarketCycle
from nanodelta.strategies import (
    PostgresStrategyRegistry,
    StrategyRuntimeCatalog,
    builtin_strategies,
)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for realtime mode")
    return value


def _secret(name: str) -> str:
    path = Path(_required(name))
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"secret file configured by {name} is empty")
    return value


def _list(name: str) -> tuple[str, ...]:
    values = tuple(value.strip() for value in _required(name).split(",") if value.strip())
    if not values:
        raise RuntimeError(f"{name} contains no symbols")
    return values


def _mapping(name: str) -> dict[str, str]:
    payload = json.loads(_required(name))
    if not isinstance(payload, dict) or not payload:
        raise RuntimeError(f"{name} must be a non-empty JSON object")
    result = {str(key).strip(): str(value).strip() for key, value in payload.items()}
    if any(not key or not value for key, value in result.items()):
        raise RuntimeError(f"{name} cannot contain empty keys or values")
    return result


def _non_negative(name: str, default: float) -> float:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric") from exc
    if not math.isfinite(value) or value < 0:
        raise RuntimeError(f"{name} must be finite and non-negative")
    return value


def build_realtime_cycles(
    database_url: str,
) -> dict[Market, Callable[[Market], Awaitable[None]]]:
    """Build three equal market cycles. No client exposes live-order methods."""
    def connect() -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(database_url)
    pipeline = EtlPipeline(PostgresStore(connect))
    registry = default_provider_registry()
    strategy_registry = PostgresStrategyRegistry(connect)
    catalog = StrategyRuntimeCatalog()
    for strategy in builtin_strategies():
        strategy_registry.register(strategy.definition)
        catalog.register(strategy)

    allocation = build_allocation_policy()
    decision_service = PaperDecisionService(
        connect=connect,
        registry=strategy_registry,
        catalog=catalog,
        ledger=PostgresDecisionLedger(connect),
        risk=RiskEngine(build_risk_limits()),
        execution=PostgresPaperExecutionEngine(
            ExecutionPolicy(
                _non_negative("NANODELTA_PAPER_SLIPPAGE_BPS", 2),
                _non_negative("NANODELTA_PAPER_FEE_BPS", 1),
            ),
            connect,
        ),
        allocation=allocation,
        account_id=os.environ.get("NANODELTA_PAPER_ACCOUNT_ID", "paper-default").strip(),
        equity=allocation.equity,
    )
    canonical_to_dhan_id = _mapping("NSE_DHAN_SECURITY_IDS_JSON")
    dhan_id_to_canonical = {value: key for key, value in canonical_to_dhan_id.items()}
    nse_symbols = tuple(canonical_to_dhan_id)
    forex_symbols = _list("FOREX_SYMBOLS")
    crypto_symbols = _list("CRYPTO_SYMBOLS")

    nse = RealtimeMarketCycle(
        Market.NSE,
        registry,
        {
            Provider.TRUEDATA: TrueDataClient(
                username=_required("TRUEDATA_USERNAME"), password=_secret("TRUEDATA_PASSWORD_PATH")
            ),
            Provider.DHAN: DhanClient(
                client_id=_required("DHAN_CLIENT_ID"),
                access_token=_secret("DHAN_ACCESS_TOKEN_PATH"),
                security_ids=canonical_to_dhan_id,
            ),
        },
        nse_symbols,
        {Provider.TRUEDATA: "ticks", Provider.DHAN: "quote"},
        pipeline,
        symbol_maps={Provider.DHAN: dhan_id_to_canonical},
        subscription_symbols={Provider.DHAN: tuple(canonical_to_dhan_id.values())},
        on_features=decision_service.process,
    )
    forex = RealtimeMarketCycle(
        Market.FOREX,
        registry,
        {
            Provider.OANDA: OandaClient(
                account_id=_required("OANDA_ACCOUNT_ID"),
                access_token=_secret("OANDA_ACCESS_TOKEN_PATH"),
                practice=os.environ.get("OANDA_ENVIRONMENT", "practice") == "practice",
            )
        },
        forex_symbols,
        {Provider.OANDA: "pricing"},
        pipeline,
        on_features=decision_service.process,
    )
    crypto = RealtimeMarketCycle(
        Market.CRYPTO,
        registry,
        {Provider.OKX: OkxClient(), Provider.POLONIEX: PoloniexClient()},
        crypto_symbols,
        {Provider.OKX: "tickers", Provider.POLONIEX: "ticker"},
        pipeline,
        on_features=decision_service.process,
    )
    return {Market.NSE: nse, Market.FOREX: forex, Market.CRYPTO: crypto}
