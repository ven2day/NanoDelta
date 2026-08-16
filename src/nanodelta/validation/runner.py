"""Opt-in credentialed NSE validation runner and explicit promotion command."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import psycopg

from nanodelta.contracts import Market, Provider
from nanodelta.history import BackfillEngine, HistoryJob, PostgresHistoryState
from nanodelta.history.timeframes import MarketCalendar
from nanodelta.persistence.migrations import Connection
from nanodelta.persistence.postgres import PostgresStore
from nanodelta.pipeline import EtlPipeline
from nanodelta.providers.base import HistoricalClient
from nanodelta.providers.registry import default_provider_registry
from nanodelta.strategies import PostgresStrategyRegistry, StrategyPlugin, technical_strategies
from nanodelta.universe import DhanNseUniverseBuilder
from nanodelta.validation.nse import NseCostModel, NseValidationCampaign, NseValidationConfig
from nanodelta.validation.postgres import PostgresNseValidationStore
from nanodelta.validation.service import NseValidationService


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _credential_environment(values: Mapping[str, str]) -> dict[str, str]:
    result = {
        "DHAN_CLIENT_ID": _required(values, "DHAN_CLIENT_ID"),
        "NSE_SYMBOLS_CSV": _required(values, "NSE_SYMBOLS_CSV"),
    }
    token_path = values.get("DHAN_ACCESS_TOKEN_PATH", "").strip()
    if token_path:
        token = Path(token_path).read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError("DHAN_ACCESS_TOKEN_PATH points to an empty secret")
        result["DHAN_ACCESS_TOKEN"] = token
    elif values.get("DHAN_ACCESS_TOKEN", "").strip():
        result["DHAN_ACCESS_TOKEN"] = values["DHAN_ACCESS_TOKEN"].strip()
    else:
        result["DHAN_PIN_PATH"] = _required(values, "DHAN_PIN_PATH")
        result["DHAN_TOTP_SECRET_PATH"] = _required(values, "DHAN_TOTP_SECRET_PATH")
    return result


def _connect(database_url: str) -> Connection:
    return cast(Connection, psycopg.connect(database_url))


def _nse_plugins() -> tuple[StrategyPlugin, ...]:
    plugins = tuple(
        cast(StrategyPlugin, plugin)
        for plugin in technical_strategies()
        if plugin.definition.identity.market is Market.NSE
    )
    expected = {"vwap_pullback", "ema_rsi_continuation", "supertrend_adx"}
    if {plugin.definition.identity.strategy_id for plugin in plugins} != expected:
        raise RuntimeError("the exact three NSE technical strategies were not found")
    return plugins


async def _backfill(
    *,
    database_url: str,
    values: Mapping[str, str],
    as_of: datetime,
    concurrency: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    builder, symbols_csv = DhanNseUniverseBuilder.from_environment(_credential_environment(values))
    universe = await builder.build(symbols_csv, now=as_of)

    def connect() -> Connection:
        return _connect(database_url)

    engine = BackfillEngine(
        pipeline=EtlPipeline(PostgresStore(connect)),
        registry=default_provider_registry(),
        clients={Provider.DHAN: cast(HistoricalClient, universe.client)},
        state=PostgresHistoryState(connect),
        calendars={Market.NSE: MarketCalendar(Market.NSE)},
    )
    jobs = tuple(
        HistoryJob(
            Market.NSE,
            instrument.symbol,
            timeframe,
            {Provider.DHAN: instrument.symbol},
            target_days=760,
            request_limit=5000,
            window_start=as_of - timedelta(days=760),
            window_end=as_of,
        )
        for instrument in universe.instruments
        for timeframe in ("5m", "15m", "30m", "1h")
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def sync(job: HistoryJob) -> str:
        async with semaphore:
            run = await engine.sync(job, now=as_of)
            return run.state.value

    states = tuple(await asyncio.gather(*(sync(job) for job in jobs)))
    return states, tuple(instrument.symbol for instrument in universe.instruments)


async def _validate(args: argparse.Namespace, values: Mapping[str, str]) -> None:
    if values.get("NANODELTA_ENABLE_CREDENTIALED_NSE_VALIDATION", "").lower() != "true":
        raise RuntimeError(
            "set NANODELTA_ENABLE_CREDENTIALED_NSE_VALIDATION=true to allow provider calls"
        )
    database_url = args.database_url or _required(values, "DATABASE_URL")
    as_of = (
        datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        if args.as_of
        else datetime.now(UTC)
    )
    if as_of.tzinfo is None:
        raise ValueError("--as-of must include a timezone")
    config = NseValidationConfig(
        cost_model=NseCostModel(
            args.brokerage_bps,
            args.taxes_and_fees_bps,
            args.slippage_bps,
        )
    )
    states, symbols = await _backfill(
        database_url=database_url,
        values=values,
        as_of=as_of,
        concurrency=args.concurrency,
    )
    campaign = NseValidationCampaign.create(
        evaluated_at=as_of,
        symbols=symbols,
        config=config,
    )

    def connect() -> Connection:
        return _connect(database_url)

    service = NseValidationService(
        store=PostgresNseValidationStore(connect),
        registry=PostgresStrategyRegistry(connect),
    )
    evidence = service.validate(campaign, _nse_plugins())
    print(
        json.dumps(
            {
                "campaign_id": campaign.campaign_id,
                "history_jobs": {
                    "total": len(states),
                    "succeeded": sum(state == "SUCCEEDED" for state in states),
                    "failed": sum(state == "FAILED" for state in states),
                },
                "strategies": [
                    {
                        "evidence_id": item.evidence_id,
                        "strategy_id": item.validation.identity.strategy_id,
                        "timeframe": item.validation.identity.timeframe,
                        "state": item.state.value,
                        "passed": item.validation.passed,
                        "rejection_reasons": item.validation.rejection_reasons,
                        "metrics": asdict(item.validation.metrics),
                    }
                    for item in evidence
                ],
                "approval_created": False,
            },
            default=str,
            sort_keys=True,
        )
    )


def _promote(args: argparse.Namespace, values: Mapping[str, str]) -> None:
    database_url = args.database_url or _required(values, "DATABASE_URL")
    approved_at = datetime.now(UTC)

    def connect() -> Connection:
        return _connect(database_url)

    service = NseValidationService(
        store=PostgresNseValidationStore(connect),
        registry=PostgresStrategyRegistry(connect),
    )
    approval = service.promote(
        evidence_id=args.evidence_id,
        reviewed_by=args.reviewed_by,
        reason=args.reason,
        approved_at=approved_at,
        expires_at=approved_at + timedelta(days=args.days),
    )
    print(json.dumps(asdict(approval), default=str, sort_keys=True))


def main(environ: Mapping[str, str] | None = None) -> None:
    values = environ or os.environ
    parser = argparse.ArgumentParser(
        description="Credentialed NSE research validation and explicit paper admission"
    )
    parser.add_argument("--database-url", default="")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--as-of", default="")
    validate.add_argument("--concurrency", type=int, default=2)
    validate.add_argument("--brokerage-bps", type=float, default=3.0)
    validate.add_argument("--taxes-and-fees-bps", type=float, default=7.0)
    validate.add_argument("--slippage-bps", type=float, default=5.0)
    promote = subparsers.add_parser("promote")
    promote.add_argument("--evidence-id", required=True)
    promote.add_argument("--reviewed-by", required=True)
    promote.add_argument("--reason", required=True)
    promote.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    if getattr(args, "concurrency", 1) < 1 or getattr(args, "days", 1) < 1:
        parser.error("concurrency and approval days must be positive")
    if args.command == "validate":
        asyncio.run(_validate(args, values))
    else:
        _promote(args, values)
