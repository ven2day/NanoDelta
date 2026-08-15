"""Market-isolated Outcome repository for closed paper trades."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.config.secrets import redact_secrets
from src.core.models import Market
from src.core.outcomes.record import OutcomeRecord

logger = logging.getLogger(__name__)


class SchemaBoundOutcomeRepository:
    def __init__(self, engine: Engine, *, market: Market | str, provider: str) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("Outcome persistence requires PostgreSQL")
        self.engine = engine
        self.market = Market.parse(market)
        self.provider = provider.strip().upper()
        self.schema = self.market.value.lower()
        self._ensure_schema()

    @property
    def table(self) -> str:
        return f'"{self.schema}"."outcome_records"'

    def _ensure_schema(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"'))
            connection.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {self.table} ("
                    "outcome_id TEXT PRIMARY KEY, outcome_version TEXT NOT NULL, trade_id TEXT "
                    "NOT NULL, decision_id TEXT, entry_execution_id TEXT, exit_execution_id TEXT, "
                    "feature_snapshot_id TEXT, provider TEXT NOT NULL, symbol TEXT NOT NULL, "
                    "timeframe TEXT NOT NULL, side TEXT NOT NULL, strategy TEXT NOT NULL, "
                    "closed_at TIMESTAMPTZ NOT NULL, net_pnl DOUBLE PRECISION NOT NULL, "
                    "return_pct DOUBLE PRECISION NOT NULL, is_winner BOOLEAN NOT NULL, "
                    "learning_eligible BOOLEAN NOT NULL, payload JSONB NOT NULL)"
                )
            )
            connection.execute(
                text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{self.schema}_outcome_trade "
                    f"ON {self.table} (trade_id)"
                )
            )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            redact_secrets(value), sort_keys=True, separators=(",", ":"), default=str
        )

    def persist_many(self, records: Iterable[OutcomeRecord]) -> int:
        rows = []
        for record in records:
            if record.market is not self.market:
                raise ValueError(
                    f"{self.market.value} Outcome repository cannot write {record.market.value}"
                )
            if record.provider != self.provider:
                raise ValueError(
                    f"{self.provider} Outcome repository cannot write provider {record.provider}"
                )
            rows.append(
                {
                    "outcome_id": record.outcome_id,
                    "outcome_version": record.outcome_version,
                    "trade_id": record.trade_id,
                    "decision_id": record.decision_id,
                    "entry_execution_id": record.entry_execution_id,
                    "exit_execution_id": record.exit_execution_id,
                    "feature_snapshot_id": record.feature_snapshot_id,
                    "provider": record.provider,
                    "symbol": record.symbol,
                    "timeframe": record.timeframe,
                    "side": record.side.value,
                    "strategy": record.strategy,
                    "closed_at": record.closed_at,
                    "net_pnl": record.net_pnl,
                    "return_pct": record.return_pct,
                    "is_winner": record.is_winner,
                    "learning_eligible": record.learning_eligible,
                    "payload": self._json(record.to_dict()),
                }
            )
        if not rows:
            return 0
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    f"INSERT INTO {self.table} (outcome_id, outcome_version, trade_id, "
                    "decision_id, entry_execution_id, exit_execution_id, feature_snapshot_id, "
                    "provider, symbol, timeframe, side, strategy, closed_at, net_pnl, return_pct, "
                    "is_winner, learning_eligible, payload) VALUES (:outcome_id, "
                    ":outcome_version, :trade_id, :decision_id, :entry_execution_id, "
                    ":exit_execution_id, :feature_snapshot_id, :provider, :symbol, :timeframe, "
                    ":side, :strategy, :closed_at, :net_pnl, :return_pct, :is_winner, "
                    ":learning_eligible, CAST(:payload AS jsonb)) ON CONFLICT (outcome_id) DO "
                    "UPDATE SET exit_execution_id=EXCLUDED.exit_execution_id, "
                    "net_pnl=EXCLUDED.net_pnl, return_pct=EXCLUDED.return_pct, "
                    "is_winner=EXCLUDED.is_winner, payload=EXCLUDED.payload"
                ),
                rows,
            )
            return max(0, int(result.rowcount or 0))


def persist_outcome_records(
    repository: SchemaBoundOutcomeRepository | None, records: Iterable[OutcomeRecord]
) -> int:
    if repository is None:
        return 0
    try:
        return repository.persist_many(records)
    except Exception:
        logger.warning("Outcome persistence failed; existing performance ledger remains active", exc_info=True)
        return 0
