"""PostgreSQL Raw/Bronze repository pinned to one market and provider."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.core.market_data.raw import RawMarketEvent
from src.core.models import Market, MarketProvider
from src.core.namespaces import normalize_market


class SchemaBoundRawMarketRepository:
    """Append-only, idempotent storage for exact provider market-data payloads."""

    def __init__(
        self,
        engine: Engine,
        *,
        market: Market | str,
        provider: MarketProvider | str,
    ) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("Raw market-data persistence requires PostgreSQL")
        self.engine = engine
        self.market = normalize_market(market)
        self.provider = MarketProvider(str(provider).strip().upper())
        self.schema = self.market.value.lower()
        self._ensure_schema()

    @property
    def table(self) -> str:
        return f'"{self.schema}"."raw_market_events"'

    def _ensure_schema(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"'))
            connection.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {self.table} ("
                    "event_id TEXT PRIMARY KEY, provider TEXT NOT NULL, "
                    "event_type TEXT NOT NULL, symbol TEXT NOT NULL, channel TEXT NOT NULL, "
                    "source_event_time TIMESTAMPTZ NOT NULL, received_at TIMESTAMPTZ NOT NULL, "
                    "payload_hash TEXT NOT NULL, schema_version TEXT NOT NULL, "
                    "payload JSONB NOT NULL)"
                )
            )
            connection.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_{self.schema}_raw_symbol_time "
                    f"ON {self.table} (symbol, event_type, source_event_time DESC)"
                )
            )

    def persist(self, event: RawMarketEvent) -> bool:
        if event.market is not self.market:
            raise ValueError(
                f"{self.market.value} raw repository cannot write {event.market.value} event"
            )
        if event.provider is not self.provider:
            raise ValueError(
                f"{self.provider.value} raw repository cannot write {event.provider.value} event"
            )
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    f"INSERT INTO {self.table} ("
                    "event_id, provider, event_type, symbol, channel, source_event_time, "
                    "received_at, payload_hash, schema_version, payload) VALUES ("
                    ":event_id, :provider, :event_type, :symbol, :channel, :source_event_time, "
                    ":received_at, :payload_hash, :schema_version, CAST(:payload AS jsonb)) "
                    "ON CONFLICT (event_id) DO NOTHING"
                ),
                {
                    "event_id": event.event_id,
                    "provider": event.provider.value,
                    "event_type": event.event_type.value,
                    "symbol": event.symbol,
                    "channel": event.channel,
                    "source_event_time": event.source_event_time,
                    "received_at": event.received_at,
                    "payload_hash": event.payload_hash,
                    "schema_version": event.schema_version,
                    "payload": event.payload_json(),
                },
            )
            return bool(result.rowcount)
