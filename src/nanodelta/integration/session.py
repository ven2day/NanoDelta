"""Deterministic paper-session evidence harness.

The strategy below exists only to prove wiring. It is fixture-labelled, validated
with a fixture artifact, and cannot be loaded by the production runtime catalog.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from nanodelta.contracts import AdvisoryAction
from nanodelta.decisions import InMemoryDecisionLedger
from nanodelta.integration.providers import IngestionEvidence
from nanodelta.orchestration import (
    AllocationPolicy,
    CyclePreconditions,
    PaperBatchExecutor,
    StagedDecisionPipeline,
)
from nanodelta.paper import ExecutionPolicy, PaperExecutionEngine
from nanodelta.risk import PortfolioSnapshot, RiskEngine, RiskLimits
from nanodelta.strategies import (
    StrategyApproval,
    StrategyContext,
    StrategyDefinition,
    StrategyIdentity,
    StrategyRegistry,
    StrategyRuntimeCatalog,
    StrategySignal,
    ValidationMetrics,
    ValidationPolicy,
    validate_strategy,
)


@dataclass(frozen=True)
class PaperSessionEvidence:
    evidence_version: int
    mode: str
    market: str
    provider: str
    symbol: str
    timeframe: str
    bronze_created: int
    silver_created: int
    gold_created: int
    reconciled: bool
    cycle_id: str
    candidate_count: int
    allocation_count: int
    risk_approved_count: int
    paper_order_count: int
    decision_reasons: tuple[str, ...]
    live_execution_interfaces: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _FixtureLineageStrategy:
    definition: StrategyDefinition

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]:
        return ("close" in context.features, "FIXTURE_CLOSE_REQUIRED")

    def generate(self, context: StrategyContext) -> StrategySignal:
        price = context.features["close"]
        distance = max(price * 0.01, 0.0001)
        return StrategySignal(
            AdvisoryAction.BUY,
            0.8,
            price,
            price - distance,
            price + 2 * distance,
            estimated_cost_r=0.02,
            historical_expectancy_r=0.15,
        )


def run_recorded_paper_session(ingestion: IngestionEvidence) -> PaperSessionEvidence:
    """Prove Gold lineage reaches an approved paper order; never a broker order."""
    if not ingestion.candles or ingestion.gold_created < 1:
        raise ValueError("session evidence requires reconciled Silver and Gold")
    latest = ingestion.candles[-1]
    evaluated_at = latest.open_time + timedelta(minutes=1)
    identity = StrategyIdentity(
        latest.market,
        "fixture_lineage_probe",
        "1.0.0-fixture",
        latest.timeframe,
        "integration-evidence",
        1,
    )
    definition = StrategyDefinition(identity, "fixture", (), "integration.session:_fixture")
    plugin = _FixtureLineageStrategy(definition)
    registry = StrategyRegistry()
    catalog = StrategyRuntimeCatalog()
    registry.register(definition)
    validation = validate_strategy(
        identity,
        ValidationMetrics(40, 5, 4, 0.04, 0.005, 0.1, 0.001, 1),
        ValidationPolicy(),
        evaluated_at=evaluated_at - timedelta(days=2),
    )
    registry.record_validation(validation)
    registry.record_approval(
        StrategyApproval.create(
            identity=identity,
            validation_run_id=validation.validation_run_id,
            approved_at=evaluated_at - timedelta(days=1),
            expires_at=evaluated_at + timedelta(days=1),
            approved_by="integration-harness",
            reason="deterministic wiring evidence only",
        )
    )
    catalog.register(plugin)
    ledger = InMemoryDecisionLedger()
    pipeline = StagedDecisionPipeline(
        registry=registry,
        strategies=catalog,
        ledger=ledger,
        allocation_policy=AllocationPolicy(100_000, 0.005, 10_000, 10_000, 1, 1),
    )
    context = StrategyContext(
        latest.market,
        latest.symbol,
        None,
        latest.timeframe,
        "integration-evidence",
        1,
        evaluated_at,
        (f"gold:{latest.record_id}",),
        {"close": latest.close},
    )
    result = pipeline.run(
        (context,),
        preconditions=CyclePreconditions(True, True, True, True),
        evaluated_at=evaluated_at,
        live_quotes={(latest.market, latest.symbol): latest.close},
    )
    batch = PaperBatchExecutor(
        registry=registry,
        risk=RiskEngine(RiskLimits(10_000, 10_000, 20_000, 20_000, 1_000, 1)),
        execution=PaperExecutionEngine(ExecutionPolicy(1, 1)),
        ledger=ledger,
    ).execute(
        result,
        account_id=f"evidence-{latest.market.value}",
        portfolio=PortfolioSnapshot(
            "evidence-snapshot",
            f"evidence-{latest.market.value}",
            100_000,
            0,
            (),
            evaluated_at,
        ),
        evaluated_at=evaluated_at,
    )
    selected = next(
        item for item in ingestion.attempts if item.provider is ingestion.selected_provider
    )
    decisions = ledger.for_cycle(result.cycle_id)
    return PaperSessionEvidence(
        1,
        "DETERMINISTIC_REPLAY",
        latest.market.value,
        latest.provider.value,
        latest.symbol,
        latest.timeframe,
        selected.bronze_created,
        selected.silver_created,
        ingestion.gold_created,
        ingestion.reconciled,
        result.cycle_id,
        len(result.candidates),
        len(result.allocations),
        sum(decision.approved for decision in batch.risk_decisions),
        len(batch.receipts),
        tuple(decision.reason_code for decision in decisions),
    )
