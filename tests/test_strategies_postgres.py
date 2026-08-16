"""Behavioral tests for PostgresStrategyRegistry, including cross-restart durability.

A fresh PostgresStrategyRegistry instance has an empty in-memory cache, exactly
like a freshly booted process or a second API replica. FakeStrategyDatabase is a
small in-memory stand-in for the three research.* tables so we can prove that a
*second* registry instance backed by the *same* database sees data the first
instance wrote -- the property that matters most here, since the in-memory-only
StrategyRegistry cannot provide it at all.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from nanodelta.contracts import Market
from nanodelta.strategies import (
    PostgresStrategyRegistry,
    StrategyApproval,
    StrategyDefinition,
    StrategyIdentity,
    ValidationMetrics,
    ValidationPolicy,
    validate_strategy,
)


def identity(*, version: str = "1.0.0") -> StrategyIdentity:
    return StrategyIdentity(Market.NSE, "vwap_pullback", version, "5m", "30m", 1)


def definition(item: StrategyIdentity) -> StrategyDefinition:
    return StrategyDefinition(
        item,
        "vwap_pullback",
        (("minimum_score", "8"),),
        "nanodelta.strategies.vwap:VwapPullback",
    )


def passing_validation(item: StrategyIdentity, now: datetime):
    return validate_strategy(
        item,
        ValidationMetrics(120, 5, 4, 0.012, 0.002, 0.11, 0.004, 10),
        ValidationPolicy(),
        evaluated_at=now,
    )


def approval(item: StrategyIdentity, now: datetime, validation_run_id: str) -> StrategyApproval:
    return StrategyApproval.create(
        identity=item,
        validation_run_id=validation_run_id,
        approved_at=now,
        expires_at=now + timedelta(days=30),
        approved_by="strategy-committee",
        reason="passed deterministic gates",
    )


class FakeStrategyDatabase:
    """In-memory stand-in for research.strategy_definitions/validation_runs/strategy_approvals."""

    def __init__(self) -> None:
        self.definitions: dict[str, dict[str, object]] = {}
        self.validations: dict[str, dict[str, object]] = {}
        self.approvals: dict[str, dict[str, object]] = {}

    def connect(self) -> FakeConnection:
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, db: FakeStrategyDatabase) -> None:
        self.db = db
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.db)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _definition_row(record: dict[str, object]) -> tuple[object, ...]:
    return (
        record["strategy_key"],
        record["market"],
        record["strategy_id"],
        record["strategy_version"],
        record["timeframe"],
        record["trade_horizon"],
        record["feature_set_version"],
        record["family"],
        record["parameters"],
        record["implementation_ref"],
    )


class FakeCursor:
    def __init__(self, db: FakeStrategyDatabase) -> None:
        self.db = db
        self._one: tuple[object, ...] | None = None
        self._all: list[tuple[object, ...]] = []

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self._one = None
        self._all = []
        if query.startswith("INSERT INTO research.strategy_definitions"):
            key = params[0]
            if key not in self.db.definitions:
                self.db.definitions[str(key)] = {
                    "strategy_key": params[0],
                    "market": params[1],
                    "strategy_id": params[2],
                    "strategy_version": params[3],
                    "timeframe": params[4],
                    "trade_horizon": params[5],
                    "feature_set_version": params[6],
                    "family": params[7],
                    "parameters": json.loads(str(params[8])),
                    "implementation_ref": params[9],
                }
        elif query.startswith("SELECT 1 FROM research.strategy_definitions"):
            self._one = (1,) if str(params[0]) in self.db.definitions else None
        elif query.startswith("SELECT strategy_key,market"):
            record = self.db.definitions.get(str(params[0]))
            self._one = _definition_row(record) if record is not None else None
        elif query.startswith("INSERT INTO research.validation_runs"):
            run_id = str(params[0])
            if run_id not in self.db.validations:
                self.db.validations[run_id] = {
                    "validation_run_id": params[0],
                    "strategy_key": params[1],
                    "evaluated_at": params[2],
                    "passed": params[3],
                    "metrics": json.loads(str(params[4])),
                    "policy": json.loads(str(params[5])),
                    "rejection_reasons": json.loads(str(params[6])),
                }
        elif query.startswith("SELECT v.validation_run_id"):
            record = self.db.validations.get(str(params[0]))
            self._one = self._validation_row(record) if record is not None else None
        elif query.startswith("INSERT INTO research.strategy_approvals"):
            self.db.approvals[str(params[0])] = {
                "approval_id": params[0],
                "strategy_key": params[1],
                "validation_run_id": params[2],
                "state": params[3],
                "approved_at": params[4],
                "expires_at": params[5],
                "approved_by": params[6],
                "reason": params[7],
            }
        elif "WHERE a.approval_id=%s" in query:
            record = self.db.approvals.get(str(params[0]))
            self._one = self._approval_row(record) if record is not None else None
        elif "a.strategy_key=%s AND a.state=%s" in query:
            strategy_key, state, at_le, at_gt = (str(params[0]), params[1], params[2], params[3])
            self._all = [
                self._approval_row(record)
                for record in self.db.approvals.values()
                if record["strategy_key"] == strategy_key
                and record["state"] == state
                and record["approved_at"] <= at_le
                and record["expires_at"] > at_gt
            ]
        elif query.startswith("SELECT DISTINCT d.strategy_key"):
            market, timeframe, trade_horizon, feature_set_version, state, at_le, at_gt = params
            approved_keys = {
                record["strategy_key"]
                for record in self.db.approvals.values()
                if record["state"] == state
                and record["approved_at"] <= at_le
                and record["expires_at"] > at_gt
            }
            self._all = [
                _definition_row(record)
                for record in self.db.definitions.values()
                if record["strategy_key"] in approved_keys
                and record["market"] == market
                and record["timeframe"] == timeframe
                and record["trade_horizon"] == trade_horizon
                and record["feature_set_version"] == feature_set_version
            ]
        else:
            raise AssertionError(f"unexpected query: {query}")

    def fetchone(self) -> tuple[object, ...] | None:
        return self._one

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._all

    def _validation_row(self, record: dict[str, object] | None) -> tuple[object, ...] | None:
        if record is None:
            return None
        definition_record = self.db.definitions[str(record["strategy_key"])]
        return (
            record["validation_run_id"],
            definition_record["market"],
            definition_record["strategy_id"],
            definition_record["strategy_version"],
            definition_record["timeframe"],
            definition_record["trade_horizon"],
            definition_record["feature_set_version"],
            record["evaluated_at"],
            record["passed"],
            record["metrics"],
            record["policy"],
            record["rejection_reasons"],
        )

    def _approval_row(self, record: dict[str, object] | None) -> tuple[object, ...] | None:
        if record is None:
            return None
        definition_record = self.db.definitions[str(record["strategy_key"])]
        return (
            record["approval_id"],
            definition_record["market"],
            definition_record["strategy_id"],
            definition_record["strategy_version"],
            definition_record["timeframe"],
            definition_record["trade_horizon"],
            definition_record["feature_set_version"],
            record["validation_run_id"],
            record["state"],
            record["approved_at"],
            record["expires_at"],
            record["approved_by"],
            record["reason"],
        )


def approve_full_lifecycle(
    db: FakeStrategyDatabase, item: StrategyIdentity, now: datetime
) -> StrategyApproval:
    registry = PostgresStrategyRegistry(db.connect)
    registry.register(definition(item))
    validation = passing_validation(item, now)
    registry.record_validation(validation)
    artifact = approval(item, now, validation.validation_run_id)
    registry.record_approval(artifact)
    return artifact


def test_second_replica_sees_approval_written_by_first_replica() -> None:
    """The property PostgresOperationalStore needed and the in-memory-only
    StrategyRegistry cannot provide: a fresh instance, empty in-memory cache,
    must still see durable state."""
    now = datetime(2026, 8, 15, tzinfo=UTC)
    db = FakeStrategyDatabase()
    item = identity()
    artifact = approve_full_lifecycle(db, item, now)

    second_replica = PostgresStrategyRegistry(db.connect)
    assert second_replica.require_approval(item, at=now + timedelta(days=1)) == artifact
    assert second_replica.eligible(
        market=Market.NSE, timeframe="5m", trade_horizon="30m", feature_set_version=1, at=now
    ) == (definition(item),)


def test_require_approval_still_enforces_expiry_and_unknown_identity() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    db = FakeStrategyDatabase()
    item = identity()
    approve_full_lifecycle(db, item, now)

    registry = PostgresStrategyRegistry(db.connect)
    with pytest.raises(LookupError):
        registry.require_approval(identity(version="2.0.0"), at=now)
    with pytest.raises(PermissionError):
        registry.require_approval(item, at=now + timedelta(days=31))


def test_revoke_by_a_second_replica_removes_eligibility_for_a_third() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    db = FakeStrategyDatabase()
    item = identity()
    artifact = approve_full_lifecycle(db, item, now)

    revoker = PostgresStrategyRegistry(db.connect)
    revoker.revoke(artifact.approval_id, "performance drift")

    observer = PostgresStrategyRegistry(db.connect)
    assert (
        observer.eligible(
            market=Market.NSE,
            timeframe="5m",
            trade_horizon="30m",
            feature_set_version=1,
            at=now + timedelta(days=1),
        )
        == ()
    )


def test_register_rejects_identity_mutation_against_persisted_definition() -> None:
    db = FakeStrategyDatabase()
    item = identity()
    first_process = PostgresStrategyRegistry(db.connect)
    first_process.register(definition(item))

    second_process = PostgresStrategyRegistry(db.connect)
    with pytest.raises(ValueError, match="immutable"):
        second_process.register(
            StrategyDefinition(item, "changed", (), "nanodelta.strategies.changed:Changed")
        )


def test_record_approval_refuses_failed_or_missing_validation_via_db() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    db = FakeStrategyDatabase()
    item = identity()
    writer = PostgresStrategyRegistry(db.connect)
    writer.register(definition(item))

    with pytest.raises(ValueError, match="validation artifact"):
        writer.record_approval(approval(item, now, "missing"))

    failed = validate_strategy(
        item,
        ValidationMetrics(2, 1, 0, 0.0, 0.01, 0.5, 0.5, 1),
        ValidationPolicy(),
        evaluated_at=now,
    )
    writer.record_validation(failed)

    reader = PostgresStrategyRegistry(db.connect)
    with pytest.raises(PermissionError, match="failed validation"):
        reader.record_approval(approval(item, now, failed.validation_run_id))


def test_register_rolls_back_and_reraises_when_the_write_fails() -> None:
    db = FakeStrategyDatabase()
    item = identity()

    class FailingConnection(FakeConnection):
        def cursor(self) -> FakeCursor:
            cursor = super().cursor()
            original = cursor.execute

            def execute(query: str, params: tuple[object, ...] = ()) -> None:
                if query.startswith("INSERT INTO research.strategy_definitions"):
                    raise RuntimeError("boom")
                original(query, params)

            cursor.execute = execute  # type: ignore[method-assign]
            return cursor

    def connect() -> FailingConnection:
        return FailingConnection(db)

    registry = PostgresStrategyRegistry(connect)
    with pytest.raises(RuntimeError, match="boom"):
        registry.register(definition(item))
    assert item.key not in db.definitions
