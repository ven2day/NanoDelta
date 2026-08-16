from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nanodelta.api.app import ApiServices, create_app
from nanodelta.contracts import AdvisoryAction, Market
from nanodelta.operations import Actor, OperationalStore, RuntimeController
from nanodelta.strategies import (
    StrategyDefinition,
    StrategyIdentity,
    StrategyRegistry,
    StrategySignal,
    ValidationPolicy,
)
from nanodelta.validation.nse import (
    NseCostModel,
    NseReadinessEvidence,
    NseValidationCampaign,
    NseValidationConfig,
    ResearchState,
    SettledCandle,
    evaluate_nse_readiness,
    evaluate_nse_strategy,
)
from nanodelta.validation.postgres import PromotionTarget
from nanodelta.validation.router import build_nse_validation_router
from nanodelta.validation.runner import _validate
from nanodelta.validation.service import NseValidationService

NOW = datetime(2026, 1, 2, 10, tzinfo=UTC)


class AlwaysBuy:
    definition = StrategyDefinition(
        StrategyIdentity(Market.NSE, "test_always_buy", "1.0.0", "15m", "intraday", 2),
        "test",
        (),
        "tests:AlwaysBuy",
    )

    def compatibility(self, context: object) -> tuple[bool, str]:
        return True, "COMPATIBLE"

    def generate(self, context: object) -> StrategySignal:
        features = getattr(context, "features")
        close = float(features["close"])
        return StrategySignal(AdvisoryAction.BUY, 0.8, close, close - 1, close + 2)


def candle(opened: datetime, timeframe: str, close: float) -> SettledCandle:
    return SettledCandle(
        "RELIANCE",
        timeframe,
        opened,
        close - 0.1,
        close + 0.2,
        close - 0.2,
        close,
        1000,
    )


def campaign(*, cost: NseCostModel | None = None) -> NseValidationCampaign:
    return NseValidationCampaign.create(
        evaluated_at=NOW,
        symbols=("RELIANCE",),
        config=NseValidationConfig(
            cost_model=cost or NseCostModel(1, 1, 1),
            policy=ValidationPolicy(
                minimum_trades=30,
                minimum_walk_forward_windows=5,
                minimum_profitable_window_ratio=0.6,
                family_wise_alpha=0.05,
            ),
        ),
    )


def ready(campaign_id: str) -> tuple[NseReadinessEvidence, ...]:
    return tuple(
        NseReadinessEvidence(
            f"ready-{timeframe}",
            campaign_id,
            "RELIANCE",
            timeframe,
            NOW - timedelta(days=731),
            NOW,
            50_000,
            1,
            731,
            True,
            (),
            f"fingerprint-{timeframe}",
        )
        for timeframe in ("5m", "15m", "30m", "1h")
    )


def test_readiness_requires_two_year_dense_dhan_history_for_all_timeframes() -> None:
    item = campaign()
    sparse = tuple(
        row
        for timeframe in item.config.required_timeframes
        for row in (
            candle(item.requested_start, timeframe, 100),
            candle(item.requested_end, timeframe, 101),
        )
    )
    evidence = evaluate_nse_readiness(item, sparse)
    assert len(evidence) == 4
    assert all(not row.ready for row in evidence)
    assert all("INSUFFICIENT_SETTLED_CANDLE_COVERAGE" in row.reasons for row in evidence)
    assert {row.timeframe for row in evidence} == {"5m", "15m", "30m", "1h"}
    assert all(row.minimum_settled_count > row.settled_count for row in evidence)

    with pytest.raises(ValueError, match="at least 730"):
        NseValidationConfig(minimum_history_days=729)
    with pytest.raises(ValueError, match=r"\[0.8, 1\]"):
        NseValidationConfig(minimum_session_coverage=0.79)


def test_walk_forward_is_deterministic_cost_aware_and_does_not_read_past_as_of() -> None:
    item = campaign()
    start = NOW - timedelta(days=4)
    candles = tuple(
        candle(start + timedelta(minutes=15 * index), "15m", 100 + index * 0.2)
        for index in range(300)
    )
    first = evaluate_nse_strategy(item, AlwaysBuy(), candles, ready(item.campaign_id))
    future = candle(NOW + timedelta(minutes=15), "15m", 999)
    second = evaluate_nse_strategy(item, AlwaysBuy(), (*candles, future), ready(item.campaign_id))

    assert first == second
    assert first.state is ResearchState.RESEARCH
    assert first.validation.passed
    assert first.validation.metrics.walk_forward_windows == 5
    assert first.validation.metrics.profitable_windows == 5
    assert first.validation.metrics.tested_hypotheses == 3
    assert first.validation.metrics.estimated_cost_per_trade == pytest.approx(0.0003)
    assert all(
        window.started_at <= window.ended_at for window in first.windows if window.started_at
    )


def test_missing_readiness_and_realistic_cost_failure_remain_failed_not_approved() -> None:
    item = campaign(cost=NseCostModel(100, 100, 100))
    start = NOW - timedelta(days=4)
    candles = tuple(
        candle(start + timedelta(minutes=15 * index), "15m", 100 + index * 0.02)
        for index in range(300)
    )
    evidence = evaluate_nse_strategy(item, AlwaysBuy(), candles, ())
    assert evidence.state is ResearchState.FAILED
    assert not evidence.validation.passed
    assert "MISSING_READINESS_EVIDENCE" in evidence.validation.rejection_reasons
    assert "NON_POSITIVE_COST_ADJUSTED_EXPECTANCY" in evidence.validation.rejection_reasons


class PromotionStore:
    def __init__(self, target: PromotionTarget) -> None:
        self.target = target
        self.promotions: list[str] = []

    def promotion_target(self, evidence_id: str) -> PromotionTarget:
        assert evidence_id == self.target.evidence_id
        return self.target

    def record_promotion(self, **values: object) -> str:
        self.promotions.append(str(values["approval_id"]))
        return "promotion-1"


def test_promotion_is_separate_reviewed_action_and_failed_evidence_is_refused() -> None:
    item = campaign()
    start = NOW - timedelta(days=4)
    candles = tuple(
        candle(start + timedelta(minutes=15 * index), "15m", 100 + index * 0.2)
        for index in range(300)
    )
    evidence = evaluate_nse_strategy(item, AlwaysBuy(), candles, ready(item.campaign_id))
    registry = StrategyRegistry()
    registry.register(AlwaysBuy.definition)
    registry.record_validation(evidence.validation)
    store = PromotionStore(
        PromotionTarget(
            evidence.evidence_id,
            evidence.validation.validation_run_id,
            evidence.validation.identity,
            evidence.state,
            evidence.validation.passed,
        )
    )
    service = NseValidationService(store=store, registry=registry)  # type: ignore[arg-type]
    approval = service.promote(
        evidence_id=evidence.evidence_id,
        reviewed_by="quant-reviewer@example.com",
        reason="reviewed deterministic evidence",
        approved_at=NOW,
        expires_at=NOW + timedelta(days=30),
    )
    assert store.promotions == [approval.approval_id]
    assert registry.require_approval(evidence.validation.identity, at=NOW) == approval

    failed_store = PromotionStore(replace(store.target, state=ResearchState.FAILED, passed=False))
    failed = NseValidationService(store=failed_store, registry=registry)  # type: ignore[arg-type]
    with pytest.raises(PermissionError, match="passing RESEARCH"):
        failed.promote(
            evidence_id=evidence.evidence_id,
            reviewed_by="reviewer",
            reason="must not work",
            approved_at=NOW,
            expires_at=NOW + timedelta(days=1),
        )


def test_credentialed_validation_is_opt_in_before_any_provider_call() -> None:
    args = SimpleNamespace(database_url="postgresql://unused", as_of="", concurrency=1)
    with pytest.raises(RuntimeError, match="NANODELTA_ENABLE_CREDENTIALED"):
        asyncio.run(_validate(args, {}))


class Reader:
    def __init__(self) -> None:
        self.filters: dict[str, object] = {}

    def strategies(self, **filters: object) -> tuple[dict[str, object], ...]:
        self.filters = filters
        return ({"strategy_id": "vwap_pullback", "lifecycle_state": "RESEARCH"},)

    def backtests(self, **filters: object) -> tuple[dict[str, object], ...]:
        self.filters = filters
        return ({"strategy_id": "vwap_pullback", "research_state": "RESEARCH"},)


def test_composable_authoritative_router_exposes_filtered_strategy_and_backtest_reads() -> None:
    reader = Reader()
    app = FastAPI()
    app.include_router(build_nse_validation_router(reader, operator_guard=lambda: "operator"))
    client = TestClient(app)

    response = client.get(
        "/api/nse/strategy-validation/strategies",
        params={"timeframe": "5m", "lifecycle_state": "RESEARCH", "limit": 25},
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["strategy_id"] == "vwap_pullback"
    assert reader.filters["timeframe"] == "5m"
    assert reader.filters["lifecycle_state"] == "RESEARCH"

    response = client.get(
        "/api/nse/strategy-validation/backtests",
        params={"strategy_id": "vwap_pullback", "research_state": "RESEARCH"},
    )
    assert response.status_code == 200
    assert reader.filters["strategy_id"] == "vwap_pullback"


def test_production_api_assembles_authenticated_nse_validation_routes() -> None:
    reader = Reader()
    operations = OperationalStore()
    app = create_app(
        ApiServices(
            operations=operations,
            controller=RuntimeController(operations),
            history_engines={},
            history_jobs={},
            api_keys={"viewer-key": Actor("viewer", "viewer")},
            nse_validation_reader=reader,
        )
    )
    client = TestClient(app)

    assert client.get("/api/nse/strategy-validation/strategies").status_code == 401
    response = client.get(
        "/api/nse/strategy-validation/strategies",
        headers={"X-API-Key": "viewer-key"},
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["strategy_id"] == "vwap_pullback"


def test_migration_is_scoped_append_only_evidence_and_no_global_latest_assumption() -> None:
    migration = Path("migrations/0016_nse_strategy_validation_evidence.sql").read_text()
    assert "minimum_history_days >= 730" in migration
    assert "source_provider = 'dhan'" in migration
    assert "research.nse_strategy_evidence" in migration
    assert "research.nse_strategy_promotions" in migration
    assert "research_state IN ('RESEARCH', 'FAILED')" in migration
    assert "PAPER_APPROVED" in migration
    assert "require_nse_technical_validation_evidence" in migration
