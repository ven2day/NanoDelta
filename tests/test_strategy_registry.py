from datetime import UTC, datetime, timedelta

import pytest

from nanodelta.contracts import Market
from nanodelta.strategies import (
    StrategyApproval,
    StrategyDefinition,
    StrategyIdentity,
    StrategyRegistry,
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


def test_validation_passes_cost_walk_forward_and_bonferroni_gates() -> None:
    result = validate_strategy(
        identity(),
        ValidationMetrics(120, 5, 4, 0.012, 0.002, 0.11, 0.004, 10),
        ValidationPolicy(),
        evaluated_at=datetime(2026, 8, 15, tzinfo=UTC),
    )

    assert result.passed
    assert result.rejection_reasons == ()
    assert result.metrics.net_expectancy == pytest.approx(0.01)


def test_validation_reports_every_failed_gate() -> None:
    result = validate_strategy(
        identity(),
        ValidationMetrics(5, 2, 0, 0.001, 0.002, 0.40, 0.03, 2),
        ValidationPolicy(),
        evaluated_at=datetime(2026, 8, 15, tzinfo=UTC),
    )

    assert not result.passed
    assert set(result.rejection_reasons) == {
        "INSUFFICIENT_TRADES",
        "INSUFFICIENT_WALK_FORWARD_WINDOWS",
        "NON_POSITIVE_COST_ADJUSTED_EXPECTANCY",
        "MAXIMUM_DRAWDOWN_EXCEEDED",
        "BONFERRONI_SIGNIFICANCE_FAILED",
    }


def test_registry_requires_current_approval_for_exact_identity() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    registry = StrategyRegistry()
    item = identity()
    registry.register(definition(item))
    validation = passing_validation(item, now)
    registry.record_validation(validation)
    artifact = approval(item, now, validation.validation_run_id)
    registry.record_approval(artifact)

    assert registry.require_approval(item, at=now + timedelta(days=1)) == artifact
    with pytest.raises(LookupError):
        registry.require_approval(identity(version="2.0.0"), at=now)
    with pytest.raises(PermissionError):
        registry.require_approval(item, at=now + timedelta(days=31))


def test_registry_rejects_identity_mutation_and_revocation_removes_eligibility() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    registry = StrategyRegistry()
    item = identity()
    registry.register(definition(item))
    with pytest.raises(ValueError, match="immutable"):
        registry.register(
            StrategyDefinition(item, "changed", (), "nanodelta.strategies.changed:Changed")
        )
    validation = passing_validation(item, now)
    registry.record_validation(validation)
    artifact = approval(item, now, validation.validation_run_id)
    registry.record_approval(artifact)
    registry.revoke(artifact.approval_id, "performance drift")

    assert (
        registry.eligible(
            market=Market.NSE,
            timeframe="5m",
            trade_horizon="30m",
            feature_set_version=1,
            at=now + timedelta(days=1),
        )
        == ()
    )


def test_registry_refuses_approval_for_failed_or_missing_validation() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    registry = StrategyRegistry()
    item = identity()
    registry.register(definition(item))
    with pytest.raises(ValueError, match="validation artifact"):
        registry.record_approval(approval(item, now, "missing"))

    failed = validate_strategy(
        item,
        ValidationMetrics(2, 1, 0, 0.0, 0.01, 0.5, 0.5, 1),
        ValidationPolicy(),
        evaluated_at=now,
    )
    registry.record_validation(failed)
    with pytest.raises(PermissionError, match="failed validation"):
        registry.record_approval(approval(item, now, failed.validation_run_id))
