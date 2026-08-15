"""Market-isolated append/update journal for paper executions."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.config.secrets import redact_secrets
from src.core.execution.record import ExecutionRecord, execution_record_from_result
from src.core.models import Market

logger = logging.getLogger(__name__)


class SchemaBoundExecutionRepository:
    def __init__(self, engine: Engine, *, market: Market | str, provider: str) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("Execution persistence requires PostgreSQL")
        self.engine = engine
        self.market = Market.parse(market)
        self.provider = provider.strip().upper()
        self.schema = self.market.value.lower()
        self._ensure_schema()

    @property
    def table(self) -> str:
        return f'"{self.schema}"."execution_records"'

    def _ensure_schema(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"'))
            connection.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {self.table} ("
                    "execution_id TEXT PRIMARY KEY, execution_version TEXT NOT NULL, "
                    "decision_id TEXT, intent_id TEXT NOT NULL, order_id TEXT, position_id TEXT, "
                    "provider TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL, "
                    "symbol TEXT NOT NULL, side TEXT NOT NULL, requested_quantity DOUBLE PRECISION "
                    "NOT NULL, requested_price DOUBLE PRECISION NOT NULL, filled_quantity "
                    "DOUBLE PRECISION NOT NULL, fill_price DOUBLE PRECISION NOT NULL, "
                    "payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL)"
                )
            )
            connection.execute(
                text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{self.schema}_execution_intent "
                    f"ON {self.table} (intent_id)"
                )
            )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            redact_secrets(value), sort_keys=True, separators=(",", ":"), default=str
        )

    def persist_many(self, records: Iterable[ExecutionRecord]) -> int:
        rows = []
        for record in records:
            if record.market is not self.market:
                raise ValueError(
                    f"{self.market.value} Execution repository cannot write {record.market.value}"
                )
            if record.provider != self.provider:
                raise ValueError(
                    f"{self.provider} Execution repository cannot write provider {record.provider}"
                )
            rows.append(
                {
                    "execution_id": record.execution_id,
                    "execution_version": record.execution_version,
                    "decision_id": record.decision_id,
                    "intent_id": record.order.intent_id,
                    "order_id": record.fill.order_id if record.fill else None,
                    "position_id": record.position.position_id if record.position else None,
                    "provider": record.provider,
                    "mode": record.mode.value,
                    "status": record.status.value,
                    "symbol": record.order.symbol,
                    "side": record.order.side.value,
                    "requested_quantity": record.order.requested_quantity,
                    "requested_price": record.order.requested_price,
                    "filled_quantity": record.fill.filled_quantity if record.fill else 0.0,
                    "fill_price": record.fill.fill_price if record.fill else 0.0,
                    "payload": self._json(record.to_dict()),
                    "updated_at": record.updated_at,
                }
            )
        if not rows:
            return 0
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    f"INSERT INTO {self.table} (execution_id, execution_version, decision_id, "
                    "intent_id, order_id, position_id, provider, mode, status, symbol, side, "
                    "requested_quantity, requested_price, filled_quantity, fill_price, payload, "
                    "updated_at) VALUES (:execution_id, :execution_version, :decision_id, "
                    ":intent_id, :order_id, :position_id, :provider, :mode, :status, :symbol, "
                    ":side, :requested_quantity, :requested_price, :filled_quantity, :fill_price, "
                    "CAST(:payload AS jsonb), :updated_at) ON CONFLICT (execution_id) DO UPDATE "
                    "SET order_id=EXCLUDED.order_id, position_id=EXCLUDED.position_id, "
                    "status=EXCLUDED.status, filled_quantity=EXCLUDED.filled_quantity, "
                    "fill_price=EXCLUDED.fill_price, payload=EXCLUDED.payload, "
                    "updated_at=EXCLUDED.updated_at"
                ),
                rows,
            )
            return max(0, int(result.rowcount or 0))

    def persist_runtime_result(
        self,
        *,
        intent_id: str,
        requested_price: float,
        order_type: str,
        result: Mapping[str, Any],
        decision_id: str | None = None,
    ) -> int:
        record = execution_record_from_result(
            market=self.market,
            provider=self.provider,
            intent_id=intent_id,
            requested_price=requested_price,
            order_type=order_type,
            result=result,
            decision_id=decision_id,
        )
        return self.persist_many([record])


def persist_execution_records(
    repository: SchemaBoundExecutionRepository | None, records: Iterable[ExecutionRecord]
) -> int:
    if repository is None:
        return 0
    try:
        return repository.persist_many(records)
    except Exception:
        logger.warning("Execution persistence failed; paper execution remains authoritative", exc_info=True)
        return 0
