"""Durable provider/failover/sequence state for realtime market feeds."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

from nanodelta.contracts import Market, Provider
from nanodelta.persistence.migrations import Connection


@dataclass(frozen=True)
class FeedStateRecord:
    market: Market
    active_provider: Provider
    state: str
    connected_at: datetime
    last_event_at: datetime | None
    gap_count: int
    failover_count: int
    last_error: str | None
    failed_over_at: datetime | None
    fallback_available: bool
    status_detail: str | None


class FeedStateStore(Protocol):
    def load(
        self, market: Market
    ) -> tuple[FeedStateRecord | None, dict[tuple[Provider, str], int]]: ...

    def save(self, record: FeedStateRecord) -> None: ...

    def save_sequence(
        self, market: Market, provider: Provider, symbol: str, sequence: int, gap_delta: int
    ) -> None: ...


class PostgresFeedStateStore:
    """Persists continuity state without storing provider credentials or payloads."""

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def load(
        self, market: Market
    ) -> tuple[FeedStateRecord | None, dict[tuple[Provider, str], int]]:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT active_provider,state,connected_at,last_event_at,gap_count,failover_count,"
                "last_error,failed_over_at,fallback_available,status_detail "
                "FROM control.realtime_feed_state WHERE market=%s",
                (market.value,),
            )
            row = cursor.fetchone()
            record = None
            if row is not None:
                record = FeedStateRecord(
                    market,
                    Provider(str(row[0])),
                    str(row[1]),
                    cast(datetime, row[2]),
                    cast(datetime | None, row[3]),
                    int(cast(int, row[4])),
                    int(cast(int, row[5])),
                    str(row[6]) if row[6] is not None else None,
                    cast(datetime | None, row[7]),
                    bool(row[8]),
                    str(row[9]) if row[9] is not None else None,
                )
            cursor.execute(
                "SELECT provider,symbol,last_sequence FROM control.realtime_sequence_state "
                "WHERE market=%s",
                (market.value,),
            )
            sequences = {
                (Provider(str(provider)), str(symbol)): int(cast(int, sequence))
                for provider, symbol, sequence in cursor.fetchall()
            }
            return record, sequences
        finally:
            connection.close()

    def save(self, record: FeedStateRecord) -> None:
        connection = self._connect()
        try:
            connection.cursor().execute(
                "INSERT INTO control.realtime_feed_state(market,active_provider,state,connected_at,"
                "last_event_at,gap_count,failover_count,last_error,failed_over_at,fallback_available,"
                "status_detail,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) "
                "ON CONFLICT(market) DO UPDATE SET active_provider=EXCLUDED.active_provider,"
                "state=EXCLUDED.state,connected_at=EXCLUDED.connected_at,"
                "last_event_at=EXCLUDED.last_event_at,gap_count=EXCLUDED.gap_count,"
                "failover_count=EXCLUDED.failover_count,last_error=EXCLUDED.last_error,"
                "failed_over_at=EXCLUDED.failed_over_at,"
                "fallback_available=EXCLUDED.fallback_available,"
                "status_detail=EXCLUDED.status_detail,updated_at=now()",
                (
                    record.market.value,
                    record.active_provider.value,
                    record.state,
                    record.connected_at,
                    record.last_event_at,
                    record.gap_count,
                    record.failover_count,
                    record.last_error,
                    record.failed_over_at,
                    record.fallback_available,
                    record.status_detail,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save_sequence(
        self, market: Market, provider: Provider, symbol: str, sequence: int, gap_delta: int
    ) -> None:
        connection = self._connect()
        try:
            connection.cursor().execute(
                "INSERT INTO control.realtime_sequence_state"
                "(market,provider,symbol,last_sequence,gap_count) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT(market,provider,symbol) DO UPDATE SET "
                "last_sequence=GREATEST(control.realtime_sequence_state.last_sequence,"
                "EXCLUDED.last_sequence),gap_count=control.realtime_sequence_state.gap_count+"
                "EXCLUDED.gap_count,updated_at=now()",
                (market.value, provider.value, symbol, sequence, gap_delta),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
