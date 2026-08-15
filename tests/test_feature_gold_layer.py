from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.aggregation.consolidation import FeatureSnapshot
from src.core.features import FeatureRecord, SchemaBoundFeatureRepository
from src.core.indicators import IndicatorResult, Timeframe


def _snapshot(*, market: str = "NSE", provider: str = "DHAN") -> FeatureSnapshot:
    indicators = IndicatorResult(
        symbol="RELIANCE" if market == "NSE" else "EUR_USD",
        timeframe=Timeframe.M15,
        open=99,
        high=102,
        low=98,
        close=101,
        volume=1_000,
    )
    return FeatureSnapshot.create(
        indicators,
        settled_candle_timestamp="2026-08-15T12:00:00+00:00",
        feature_version="test-v1",
        market=market,
        provider=provider,
    )


class _Result:
    rowcount = 1


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def execute(self, statement: Any, params: Any = None) -> _Result:
        self.calls.append((str(statement), params))
        return _Result()


class _PostgresEngine:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self) -> None:
        self.connection = _Connection()

    @contextmanager
    def begin(self):  # type: ignore[no-untyped-def]
        yield self.connection


def test_feature_record_reuses_snapshot_identity_and_indicator_vector() -> None:
    snapshot = _snapshot()

    record = FeatureRecord.from_snapshot(snapshot)

    assert record.snapshot_id == snapshot.snapshot_id
    assert record.indicators["price"]["close"] == 101
    assert record.payload()["feature_version"] == "test-v1"


def test_feature_record_rejects_timestamp_without_timezone() -> None:
    snapshot = FeatureSnapshot.create(
        _snapshot().indicators,
        settled_candle_timestamp="2026-08-15T12:00:00",
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        FeatureRecord.from_snapshot(snapshot)


def test_feature_repository_is_market_bound_and_idempotent() -> None:
    engine = _PostgresEngine()
    repository = SchemaBoundFeatureRepository(
        engine,  # type: ignore[arg-type]
        market="NSE",
        provider="DHAN",
    )

    assert repository.persist_many([FeatureRecord.from_snapshot(_snapshot())]) == 1
    sql = "\n".join(call[0] for call in engine.connection.calls)
    assert 'INSERT INTO "nse"."feature_snapshots"' in sql
    assert "ON CONFLICT (snapshot_id) DO NOTHING" in sql

    with pytest.raises(ValueError, match="cannot write FOREX"):
        repository.persist_many(
            [FeatureRecord.from_snapshot(_snapshot(market="FOREX", provider="OANDA"))]
        )
