"""Environment composition for realtime, supervised, paper-only market cycles."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import psycopg

from nanodelta.contracts import Market, Provider
from nanodelta.decisions_postgres import PostgresDecisionLedger
from nanodelta.observability import RuntimeMetrics
from nanodelta.paper import (
    ExecutionPolicy,
    PostgresPaperExecutionEngine,
)
from nanodelta.paper.lifecycle import PaperPositionLifecycle
from nanodelta.paper.lifecycle_postgres import PostgresLifecycleStore
from nanodelta.persistence.postgres import PostgresStore
from nanodelta.pipeline import EtlPipeline
from nanodelta.providers.base import ProviderCapability, RealtimeClient
from nanodelta.providers.dhan import DhanClient
from nanodelta.providers.dhan_auth import DhanSecretFiles, DhanTokenProvider
from nanodelta.providers.oanda import OandaClient
from nanodelta.providers.okx import OkxClient
from nanodelta.providers.poloniex import PoloniexClient
from nanodelta.providers.registry import default_provider_registry
from nanodelta.providers.truedata import TrueDataClient
from nanodelta.risk import RiskEngine
from nanodelta.runtime.feed_state import PostgresFeedStateStore
from nanodelta.runtime.paper_decision import PaperDecisionService
from nanodelta.runtime.paper_policy import build_allocation_policy, build_risk_limits
from nanodelta.runtime.realtime import RealtimeMarketCycle
from nanodelta.strategies import (
    PostgresStrategyRegistry,
    StrategyPlugin,
    StrategyRuntimeCatalog,
    builtin_strategies,
    technical_strategies,
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


async def _dhan_access_token(client_id: str) -> str:
    """Option A: a manually generated 24-hour token at DHAN_ACCESS_TOKEN_PATH.
    Option B: PIN+TOTP auto-generation via DhanTokenProvider (DhanSecretFiles at
    DHAN_PIN_PATH/DHAN_TOTP_SECRET_PATH) -- generated once at process startup, which
    is enough for one trading session since Dhan tokens are valid ~24h. Prefers a
    manually supplied token when both are configured."""
    static_path = os.environ.get("DHAN_ACCESS_TOKEN_PATH", "").strip()
    if static_path:
        return _secret("DHAN_ACCESS_TOKEN_PATH")
    pin_path = os.environ.get("DHAN_PIN_PATH", "").strip()
    totp_path = os.environ.get("DHAN_TOTP_SECRET_PATH", "").strip()
    if not (pin_path and totp_path):
        raise RuntimeError(
            "Dhan credentials are required: set DHAN_ACCESS_TOKEN_PATH, or both "
            "DHAN_PIN_PATH and DHAN_TOTP_SECRET_PATH"
        )
    provider = DhanTokenProvider(
        client_id=client_id,
        secrets=DhanSecretFiles(Path(pin_path), Path(totp_path)),
    )
    token = await provider.token(now=datetime.now(UTC))
    return token.value


def _truedata_client_or_none() -> TrueDataClient | None:
    """TrueData is NSE's documented realtime primary (Dhan is its fallback), but it's
    a genuinely optional add-on, not every NSE deployment has a TrueData account.
    Absent credentials mean "not configured", not a startup failure -- the caller
    falls back to a Dhan-only realtime route for NSE in that case."""
    username = os.environ.get("TRUEDATA_USERNAME", "").strip()
    if not username:
        return None
    return TrueDataClient(username=username, password=_secret("TRUEDATA_PASSWORD_PATH"))


async def build_realtime_cycles(
    database_url: str,
    *,
    metrics: RuntimeMetrics | None = None,
) -> dict[Market, Callable[[Market], Awaitable[None]]]:
    """Build three equal market cycles. No client exposes live-order methods."""
    def connect() -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(database_url)
    pipeline = EtlPipeline(PostgresStore(connect))
    feed_state = PostgresFeedStateStore(connect)
    registry = default_provider_registry()
    strategy_registry = PostgresStrategyRegistry(connect)
    catalog = StrategyRuntimeCatalog()
    all_strategies = [
        cast(StrategyPlugin, strategy)
        for strategy in (*builtin_strategies(), *technical_strategies())
    ]
    for strategy in all_strategies:
        strategy_registry.register(strategy.definition)
        catalog.register(strategy)

    allocation = build_allocation_policy()
    risk_engine = RiskEngine(build_risk_limits())
    execution_engine = PostgresPaperExecutionEngine(
        ExecutionPolicy(
            _non_negative("NANODELTA_PAPER_SLIPPAGE_BPS", 2),
            _non_negative("NANODELTA_PAPER_FEE_BPS", 1),
        ),
        connect,
    )
    ledger = PostgresDecisionLedger(connect)
    lifecycle = PaperPositionLifecycle(
        store=PostgresLifecycleStore(connect),
        execution=execution_engine,
        risk=risk_engine,
        ledger=ledger,
    )
    decision_service = PaperDecisionService(
        connect=connect,
        registry=strategy_registry,
        catalog=catalog,
        ledger=ledger,
        risk=risk_engine,
        execution=execution_engine,
        allocation=allocation,
        account_id=os.environ.get("NANODELTA_PAPER_ACCOUNT_ID", "paper-default").strip(),
        equity=allocation.equity,
        metrics=metrics,
        lifecycle=lifecycle,
    )
    canonical_to_dhan_id = _mapping("NSE_DHAN_SECURITY_IDS_JSON")
    dhan_id_to_canonical = {value: key for key, value in canonical_to_dhan_id.items()}
    nse_symbols = tuple(canonical_to_dhan_id)
    forex_symbols = _list("FOREX_SYMBOLS")
    crypto_symbols = _list("CRYPTO_SYMBOLS")

    dhan_client_id = _required("DHAN_CLIENT_ID")
    dhan_client = DhanClient(
        client_id=dhan_client_id,
        access_token=await _dhan_access_token(dhan_client_id),
        security_ids=canonical_to_dhan_id,
    )
    truedata_client = _truedata_client_or_none()
    nse_clients: dict[Provider, RealtimeClient] = {Provider.DHAN: dhan_client}
    nse_channels = {Provider.DHAN: "quote"}
    if truedata_client is not None:
        nse_clients[Provider.TRUEDATA] = truedata_client
        nse_channels[Provider.TRUEDATA] = "ticks"
    else:
        # No TrueData account configured -- Dhan is NSE realtime's documented
        # fallback; route to it alone instead of failing startup over an optional
        # secondary provider.
        registry.register(Market.NSE, ProviderCapability.REALTIME_QUOTES, (Provider.DHAN,))
    nse = RealtimeMarketCycle(
        Market.NSE,
        registry,
        nse_clients,
        nse_symbols,
        nse_channels,
        pipeline,
        symbol_maps={Provider.DHAN: dhan_id_to_canonical},
        subscription_symbols={Provider.DHAN: tuple(canonical_to_dhan_id.values())},
        on_features=decision_service.process,
        metrics=metrics,
        state_store=feed_state,
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
        metrics=metrics,
        state_store=feed_state,
    )
    crypto = RealtimeMarketCycle(
        Market.CRYPTO,
        registry,
        {Provider.OKX: OkxClient(), Provider.POLONIEX: PoloniexClient()},
        crypto_symbols,
        {Provider.OKX: "tickers", Provider.POLONIEX: "ticker"},
        pipeline,
        on_features=decision_service.process,
        metrics=metrics,
        state_store=feed_state,
    )
    return {Market.NSE: nse, Market.FOREX: forex, Market.CRYPTO: crypto}
