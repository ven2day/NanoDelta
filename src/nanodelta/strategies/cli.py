"""Operator CLI for evidence generation and explicit strategy admission."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import cast

import psycopg

from nanodelta.contracts import Market
from nanodelta.persistence.migrations import Connection
from nanodelta.strategies import (
    MomentumContinuationStrategy,
    PostgresStrategyRegistry,
    StrategyApproval,
    ValidationPolicy,
    builtin_strategies,
)
from nanodelta.strategies.evaluation import PostgresStrategyEvaluator


def _plugin(market: Market, strategy_id: str) -> MomentumContinuationStrategy:
    matches = [
        plugin
        for plugin in builtin_strategies()
        if plugin.definition.identity.market is market
        and plugin.definition.identity.strategy_id == strategy_id
    ]
    if len(matches) != 1:
        raise LookupError("exact built-in strategy plugin was not found")
    return matches[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and govern NanoDelta strategies")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    approve = subparsers.add_parser("approve")
    for command in (validate, approve):
        command.add_argument("--market", type=Market, required=True)
        command.add_argument("--strategy", default="momentum_continuation")
    validate.add_argument("--round-trip-cost", type=float, required=True)
    validate.add_argument("--tested-hypotheses", type=int, required=True)
    approve.add_argument("--validation-run-id", required=True)
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--reason", required=True)
    approve.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    if not args.database_url:
        parser.error("DATABASE_URL or --database-url is required")

    def connect() -> Connection:
        return cast(Connection, psycopg.connect(args.database_url))
    registry = PostgresStrategyRegistry(connect)
    plugin = _plugin(args.market, args.strategy)
    registry.register(plugin.definition)
    now = datetime.now(UTC)
    if args.command == "validate":
        result = PostgresStrategyEvaluator(connect, registry).evaluate(
            plugin,
            policy=ValidationPolicy(),
            estimated_round_trip_cost=args.round_trip_cost,
            tested_hypotheses=args.tested_hypotheses,
            evaluated_at=now,
        )
        print(json.dumps(asdict(result), default=str, sort_keys=True))
        return
    if args.days < 1:
        parser.error("--days must be positive")
    approval = StrategyApproval.create(
        identity=plugin.definition.identity,
        validation_run_id=args.validation_run_id,
        approved_at=now,
        expires_at=now + timedelta(days=args.days),
        approved_by=args.approved_by,
        reason=args.reason,
    )
    registry.record_approval(approval)
    print(json.dumps(asdict(approval), default=str, sort_keys=True))


if __name__ == "__main__":
    main()
