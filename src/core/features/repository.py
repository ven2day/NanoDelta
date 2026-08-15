"""Market-isolated PostgreSQL repository for Feature/Gold snapshots."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.config.secrets import redact_secrets
from src.core.features.record import FeatureRecord
from src.core.models import Market

logger = logging.getLogger(__name__)


class SchemaBoundFeatureRepository:
    """Idempotent Gold storage pinned to exactly one market and provider."""

    def __init__(self, engine: Engine, *, market: Market | str, provider: str) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("Feature persistence requires PostgreSQL")
        self.engine = engine
        self.market = Market.parse(market)
        self.provider = provider.strip().upper()
        self.schema = self.market.value.lower()
        self._ensure_schema()

    @property
    def table(self) -> str:
        return f'"{self.schema}"."feature_snapshots"'

    def _ensure_schema(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"'))
            connection.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {self.table} ("
                    "snapshot_id TEXT PRIMARY KEY, provider TEXT NOT NULL, symbol TEXT NOT NULL, "
                    "timeframe TEXT NOT NULL, settled_candle_timestamp TIMESTAMPTZ NOT NULL, "
                    "feature_version TEXT NOT NULL, volume_type TEXT NOT NULL, "
                    "indicators JSONB NOT NULL, market_relative JSONB, payload JSONB NOT NULL, "
                    "created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
            )
            connection.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_{self.schema}_feature_symbol_time "
                    f"ON {self.table} (symbol, timeframe, settled_candle_timestamp DESC)"
                )
            )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            redact_secrets(value),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )

    def persist_many(self, records: Iterable[FeatureRecord]) -> int:
        rows = []
        for record in records:
            if record.market is not self.market:
                raise ValueError(
                    f"{self.market.value} feature repository cannot write {record.market.value}"
                )
            if record.provider != self.provider:
                raise ValueError(
                    f"{self.provider} feature repository cannot write provider {record.provider}"
                )
            rows.append(
                {
                    "snapshot_id": record.snapshot_id,
                    "provider": record.provider,
                    "symbol": record.symbol,
                    "timeframe": record.timeframe,
                    "settled_candle_timestamp": record.settled_candle_timestamp,
                    "feature_version": record.feature_version,
                    "volume_type": record.volume_type,
                    "indicators": self._json(dict(record.indicators)),
                    "market_relative": (
                        self._json(dict(record.market_relative))
                        if record.market_relative is not None
                        else None
                    ),
                    "payload": self._json(record.payload()),
                }
            )
        if not rows:
            return 0
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    f"INSERT INTO {self.table} (snapshot_id, provider, symbol, timeframe, "
                    "settled_candle_timestamp, feature_version, volume_type, indicators, "
                    "market_relative, payload) VALUES (:snapshot_id, :provider, :symbol, "
                    ":timeframe, :settled_candle_timestamp, :feature_version, :volume_type, "
                    "CAST(:indicators AS jsonb), CAST(:market_relative AS jsonb), "
                    "CAST(:payload AS jsonb)) ON CONFLICT (snapshot_id) DO NOTHING"
                ),
                rows,
            )
            return int(result.rowcount or 0)


def persist_feature_snapshots(
    repository: SchemaBoundFeatureRepository | None,
    snapshots: Iterable[Any],
) -> int:
    """Persist a batch without allowing optional Gold storage to stop trading."""

    if repository is None:
        return 0
    try:
        return repository.persist_many(FeatureRecord.from_snapshot(item) for item in snapshots)
    except Exception:
        logger.warning("Feature/Gold persistence failed; trading cycle continues", exc_info=True)
        return 0
