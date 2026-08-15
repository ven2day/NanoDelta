"""Durable PostgreSQL history state behind the orchestration protocol."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from nanodelta.contracts import Market, Provider
from nanodelta.history.engine import HistoryRun, HistoryRunState, Watermark
from nanodelta.persistence.migrations import Connection


class PostgresHistoryState:
    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def _query(
        self, query: str, params: tuple[object, ...], *, many: bool = False
    ) -> list[tuple[object, ...]]:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(query, params)
            if many:
                return cursor.fetchall()
            row = cursor.fetchone()
            return [row] if row is not None else []
        finally:
            connection.close()

    def _write(self, query: str, params: tuple[object, ...]) -> None:
        connection = self._connect()
        try:
            connection.cursor().execute(query, params)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def covered(self, market: Market, symbol: str, timeframe: str) -> set[datetime]:
        schema = f"{market.value}_silver"
        rows = self._query(
            f"SELECT open_time FROM {schema}.candles "
            "WHERE symbol = %s AND timeframe = %s AND is_settled = true",
            (symbol, timeframe),
            many=True,
        )
        return {row[0] for row in rows if isinstance(row[0], datetime)}

    def record_open(self, market: Market, symbol: str, timeframe: str, value: datetime) -> None:
        del market, symbol, timeframe, value
        # The ETL transaction already persisted the canonical candle.

    def watermark(
        self, market: Market, provider: Provider, symbol: str, timeframe: str
    ) -> Watermark | None:
        rows = self._query(
            "SELECT event_time_watermark, updated_at FROM control.provider_watermarks "
            "WHERE market=%s AND provider=%s AND dataset='candles' "
            "AND symbol=%s AND timeframe=%s",
            (market.value, provider.value, symbol, timeframe),
        )
        if not rows or not isinstance(rows[0][0], datetime) or not isinstance(rows[0][1], datetime):
            return None
        return Watermark(market, provider, symbol, timeframe, rows[0][0], rows[0][1])

    def commit_watermark(self, value: Watermark) -> None:
        self._write(
            "INSERT INTO control.provider_watermarks "
            "(market,provider,dataset,symbol,timeframe,event_time_watermark,updated_at) "
            "VALUES (%s,%s,'candles',%s,%s,%s,%s) "
            "ON CONFLICT (market,provider,dataset,symbol,timeframe) DO UPDATE SET "
            "event_time_watermark=GREATEST(control.provider_watermarks.event_time_watermark,"
            "EXCLUDED.event_time_watermark), updated_at=EXCLUDED.updated_at",
            (
                value.market.value,
                value.provider.value,
                value.symbol,
                value.timeframe,
                value.event_time,
                value.updated_at,
            ),
        )

    def save_run(self, run: HistoryRun) -> None:
        self._write(
            "INSERT INTO control.history_runs "
            "(run_id,market,symbol,timeframe,state,started_at,finished_at,provider,"
            "rows_received,bronze_created,silver_created,error_message) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (run_id) DO UPDATE SET state=EXCLUDED.state,"
            "finished_at=EXCLUDED.finished_at,provider=EXCLUDED.provider,"
            "rows_received=EXCLUDED.rows_received,bronze_created=EXCLUDED.bronze_created,"
            "silver_created=EXCLUDED.silver_created,error_message=EXCLUDED.error_message",
            (
                run.run_id,
                run.market.value,
                run.symbol,
                run.timeframe,
                run.state.value,
                run.started_at,
                run.finished_at,
                run.provider.value if run.provider else None,
                run.rows_received,
                run.bronze_created,
                run.silver_created,
                run.error,
            ),
        )

    def has_run_state(
        self, market: Market, symbol: str, timeframe: str, state: HistoryRunState
    ) -> bool:
        rows = self._query(
            "SELECT 1 FROM control.history_runs WHERE market=%s AND symbol=%s "
            "AND timeframe=%s AND state=%s LIMIT 1",
            (market.value, symbol, timeframe, state.value),
        )
        return bool(rows)
