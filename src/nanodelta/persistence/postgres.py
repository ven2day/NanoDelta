"""Market-isolated PostgreSQL/TimescaleDB record store."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from nanodelta.contracts import Market
from nanodelta.persistence.migrations import Connection


class PostgresStore:
    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def write(
        self,
        *,
        market: Market,
        layer: str,
        event_time: datetime,
        record_id: str,
        record: Mapping[str, Any],
    ) -> bool:
        del event_time
        if layer == "bronze":
            query, params = self._bronze_insert(market, record_id, record)
        elif layer == "silver":
            query, params = self._silver_insert(market, record)
        elif layer == "gold":
            query, params = self._gold_insert(market, record_id, record)
        else:
            raise ValueError(f"unsupported layer: {layer}")

        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(query, params)
            created = cursor.fetchone()
            connection.commit()
            return created is not None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _schema(market: Market, layer: str) -> str:
        return f"{market.value}_{layer}"

    def _bronze_insert(
        self, market: Market, record_id: str, record: Mapping[str, Any]
    ) -> tuple[str, tuple[object, ...]]:
        payload = record.get("payload")
        payload_text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        import hashlib

        payload_hash = hashlib.sha256(payload_text.encode()).hexdigest()
        query = (
            f"INSERT INTO {self._schema(market, 'bronze')}.raw_events "
            "(record_id, provider, event_type, provider_symbol, received_at, "
            "schema_version, payload_hash, payload) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
            "ON CONFLICT (record_id) DO NOTHING RETURNING record_id"
        )
        params: tuple[object, ...] = (
            record_id,
            record["provider"],
            record["event_type"],
            record["provider_symbol"],
            record["received_at"],
            record["schema_version"],
            payload_hash,
            payload_text,
        )
        return query, params

    def _silver_insert(
        self, market: Market, record: Mapping[str, Any]
    ) -> tuple[str, tuple[object, ...]]:
        query = (
            f"INSERT INTO {self._schema(market, 'silver')}.candles "
            "(symbol, timeframe, open_time, provider, raw_record_id, open, high, low, "
            "close, volume, is_settled, schema_version) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (symbol, timeframe, open_time) DO NOTHING RETURNING raw_record_id"
        )
        keys = (
            "symbol",
            "timeframe",
            "open_time",
            "provider",
            "raw_record_id",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "is_settled",
            "schema_version",
        )
        return query, tuple(record[key] for key in keys)

    def _gold_insert(
        self, market: Market, record_id: str, record: Mapping[str, Any]
    ) -> tuple[str, tuple[object, ...]]:
        features = {
            key: value
            for key, value in record.items()
            if key
            not in {
                "record_id",
                "candle_record_id",
                "market",
                "symbol",
                "timeframe",
                "event_time",
                "feature_version",
            }
        }
        query = (
            f"INSERT INTO {self._schema(market, 'gold')}.feature_snapshots "
            "(record_id, candle_record_id, symbol, timeframe, event_time, feature_version, "
            "features) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) "
            "ON CONFLICT (record_id, event_time) DO NOTHING RETURNING record_id"
        )
        params: tuple[object, ...] = (
            record_id,
            record["candle_record_id"],
            record["symbol"],
            record["timeframe"],
            record["event_time"],
            record["feature_version"],
            json.dumps(features, sort_keys=True, separators=(",", ":")),
        )
        return query, params
