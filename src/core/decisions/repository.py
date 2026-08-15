"""Market-isolated PostgreSQL Decision journal."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.config.secrets import redact_secrets
from src.core.decisions.record import DecisionRecord
from src.core.models import Market

logger = logging.getLogger(__name__)


class SchemaBoundDecisionRepository:
    def __init__(self, engine: Engine, *, market: Market | str, provider: str) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("Decision persistence requires PostgreSQL")
        self.engine = engine
        self.market = Market.parse(market)
        self.provider = provider.strip().upper()
        self.schema = self.market.value.lower()
        self._ensure_schema()

    @property
    def table(self) -> str:
        return f'"{self.schema}"."decision_records"'

    def _ensure_schema(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"'))
            connection.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {self.table} ("
                    "decision_id TEXT PRIMARY KEY, decision_version TEXT NOT NULL, "
                    "candidate_id TEXT NOT NULL, feature_snapshot_id TEXT, provider TEXT NOT NULL, "
                    "symbol TEXT NOT NULL, timeframe TEXT NOT NULL, side TEXT NOT NULL, "
                    "settled_candle_timestamp TIMESTAMPTZ NOT NULL, status TEXT NOT NULL, "
                    "final_action TEXT NOT NULL, rejection_reasons JSONB NOT NULL, "
                    "evidence JSONB NOT NULL, payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL)"
                )
            )
            connection.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_{self.schema}_decision_symbol_time "
                    f"ON {self.table} (symbol, timeframe, settled_candle_timestamp DESC)"
                )
            )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            redact_secrets(value), sort_keys=True, separators=(",", ":"), default=str
        )

    def persist_many(self, records: Iterable[DecisionRecord]) -> int:
        rows = []
        for record in records:
            if record.market is not self.market:
                raise ValueError(
                    f"{self.market.value} Decision repository cannot write {record.market.value}"
                )
            if record.provider != self.provider:
                raise ValueError(
                    f"{self.provider} Decision repository cannot write provider {record.provider}"
                )
            rows.append(
                {
                    "decision_id": record.decision_id,
                    "decision_version": record.decision_version,
                    "candidate_id": record.candidate_id,
                    "feature_snapshot_id": record.feature_snapshot_id,
                    "provider": record.provider,
                    "symbol": record.symbol,
                    "timeframe": record.timeframe,
                    "side": record.side.value,
                    "settled_candle_timestamp": record.settled_candle_timestamp,
                    "status": record.status.value,
                    "final_action": record.final_action,
                    "rejection_reasons": self._json(list(record.rejection_reasons)),
                    "evidence": self._json([item.to_dict() for item in record.evidence]),
                    "payload": self._json(record.to_dict()),
                    "updated_at": record.updated_at,
                }
            )
        if not rows:
            return 0
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    f"INSERT INTO {self.table} (decision_id, decision_version, candidate_id, "
                    "feature_snapshot_id, provider, symbol, timeframe, side, "
                    "settled_candle_timestamp, status, final_action, rejection_reasons, evidence, "
                    "payload, updated_at) VALUES (:decision_id, :decision_version, :candidate_id, "
                    ":feature_snapshot_id, :provider, :symbol, :timeframe, :side, "
                    ":settled_candle_timestamp, :status, :final_action, "
                    "CAST(:rejection_reasons AS jsonb), CAST(:evidence AS jsonb), "
                    "CAST(:payload AS jsonb), :updated_at) ON CONFLICT (decision_id) DO UPDATE SET "
                    "status=EXCLUDED.status, final_action=EXCLUDED.final_action, "
                    "rejection_reasons=EXCLUDED.rejection_reasons, evidence=EXCLUDED.evidence, "
                    "payload=EXCLUDED.payload, updated_at=EXCLUDED.updated_at"
                ),
                rows,
            )
            return max(0, int(result.rowcount or 0))


def persist_decision_records(
    repository: SchemaBoundDecisionRepository | None, records: Iterable[DecisionRecord]
) -> int:
    if repository is None:
        return 0
    try:
        return repository.persist_many(records)
    except Exception:
        logger.warning("Decision persistence failed; trading cycle continues", exc_info=True)
        return 0
