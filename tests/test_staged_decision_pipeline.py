from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from nanodelta.agents import AdvisoryAction
from nanodelta.contracts import Market
from nanodelta.decisions import (
    Decision,
    DecisionStage,
    DecisionStatus,
    InMemoryDecisionLedger,
)
from nanodelta.orchestration import (
    AllocationPolicy,
    CyclePreconditions,
    LlmReviewMode,
    LlmVerdict,
    PaperBatchExecutor,
    StagedDecisionPipeline,
)
from nanodelta.paper import ExecutionPolicy, PaperExecutionEngine
from nanodelta.risk import PortfolioSnapshot, RiskEngine, RiskLimits
from nanodelta.strategies import (
    RegimeEvidence,
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

NOW = datetime(2026, 8, 15, 10, 15, tzinfo=UTC)


@dataclass
class FixedStrategy:
    definition: StrategyDefinition
    action: AdvisoryAction = AdvisoryAction.BUY
    confidence: float = 0.8
    trigger: bool = True

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]:
        required = dict(self.definition.parameters).get("required_feature")
        if required and required not in context.features:
            return False, "REQUIRED_FEATURE_MISSING"
        return True, "COMPATIBLE"

    def generate(self, context: StrategyContext) -> StrategySignal | None:
        if not self.trigger:
            return None
        price = context.features["close"]
        if self.action is AdvisoryAction.BUY:
            stop, target = price - 2, price + 4
        else:
            stop, target = price + 2, price - 4
        return StrategySignal(
            self.action,
            self.confidence,
            price,
            stop,
            target,
            estimated_cost_r=0.05,
            historical_expectancy_r=0.1,
        )


def approved_registry(*plugins: FixedStrategy) -> tuple[StrategyRegistry, StrategyRuntimeCatalog]:
    registry = StrategyRegistry()
    catalog = StrategyRuntimeCatalog()
    for plugin in plugins:
        identity = plugin.definition.identity
        registry.register(plugin.definition)
        validation = validate_strategy(
            identity,
            ValidationMetrics(100, 5, 4, 0.02, 0.002, 0.1, 0.001, 5),
            ValidationPolicy(),
            evaluated_at=NOW - timedelta(days=2),
        )
        registry.record_validation(validation)
        registry.record_approval(
            StrategyApproval.create(
                identity=identity,
                validation_run_id=validation.validation_run_id,
                approved_at=NOW - timedelta(days=1),
                expires_at=NOW + timedelta(days=30),
                approved_by="committee",
                reason="validated",
            )
        )
        catalog.register(plugin)
    return registry, catalog


def plugin(name: str, *, confidence: float = 0.8) -> FixedStrategy:
    identity = StrategyIdentity(Market.NSE, name, "1.0.0", "15m", "intraday", 1)
    return FixedStrategy(
        StrategyDefinition(identity, name, (("required_feature", "close"),), f"tests:{name}"),
        confidence=confidence,
    )


def context(
    symbol: str = "RELIANCE",
    *,
    sector: str = "ENERGY",
    regime: RegimeEvidence | None = None,
    tradeable: bool = True,
) -> StrategyContext:
    return StrategyContext(
        Market.NSE,
        symbol,
        sector,
        "15m",
        "intraday",
        1,
        NOW,
        (f"gold-{symbol}",),
        {"close": 100.0},
        tradeable=tradeable,
        tradeability_reason="MIN_TRADED_VALUE" if not tradeable else "TRADEABLE",
        regime=regime or RegimeEvidence(),
    )


def policy(**changes: float | int) -> AllocationPolicy:
    values: dict[str, float | int] = {
        "equity": 100_000,
        "risk_fraction_per_trade": 0.01,
        "max_order_notional": 20_000,
        "max_total_new_notional": 50_000,
        "max_positions": 5,
        "max_sector_positions": 2,
        "maximum_entry_drift_fraction": 0.003,
        "minimum_reward_risk": 1.0,
    }
    values.update(changes)
    return AllocationPolicy(**values)  # type: ignore[arg-type]


def pipeline(*plugins: FixedStrategy, **kwargs: object) -> StagedDecisionPipeline:
    registry, catalog = approved_registry(*plugins)
    return StagedDecisionPipeline(
        registry=registry,
        strategies=catalog,
        ledger=kwargs.pop("ledger", InMemoryDecisionLedger()),  # type: ignore[arg-type]
        allocation_policy=kwargs.pop("allocation_policy", policy()),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def normal() -> CyclePreconditions:
    return CyclePreconditions(True, True, True, True)


def test_all_approved_plugins_run_and_regime_changes_score_without_veto() -> None:
    first = plugin("vwap_pullback", confidence=0.8)
    second = plugin("breakout", confidence=0.7)
    result = pipeline(first, second).run(
        (context(regime=RegimeEvidence(0.5, 0.8, 0.7, 0.6)),),
        preconditions=normal(),
        evaluated_at=NOW,
        live_quotes={(Market.NSE, "RELIANCE"): 100.1},
    )

    assert {candidate.identity.strategy_id for candidate in result.candidates} == {
        "vwap_pullback",
        "breakout",
    }
    assert len(result.scored) == 2
    assert len(result.allocations) == 1
    assert result.allocations[0].candidate.candidate.identity.strategy_id == "vwap_pullback"


def test_generated_buy_sell_candidate_and_full_attribution_are_durable_ledger_evidence() -> None:
    ledger = InMemoryDecisionLedger()
    result = pipeline(plugin("vwap_pullback"), ledger=ledger).run(
        (context(regime=RegimeEvidence(0.7, 0.8, 0.9, 0.6)),),
        preconditions=normal(),
        evaluated_at=NOW,
        live_quotes={(Market.NSE, "RELIANCE"): 100},
    )

    candidate = ledger.candidates[result.candidates[0].candidate_id]
    assert candidate.action is AdvisoryAction.BUY
    assert candidate.reference_price == 100
    assert candidate.stop_price == 98
    assert candidate.target_price == 104
    assert candidate.gold_snapshot_ids == ("gold-RELIANCE",)
    assert dict(candidate.evidence)["mtf_alignment"] == 0.6

    signal = next(
        decision for decision in result.decisions if decision.stage is DecisionStage.SIGNAL
    )
    assert signal.detail == "BUY"
    assert dict(signal.metrics)["reference_price"] == 100
    scoring = next(
        decision for decision in result.decisions if decision.stage is DecisionStage.SCORING
    )
    metrics = dict(scoring.metrics)
    assert metrics["market_regime_fit"] == 0.7
    assert metrics["sector_regime_fit"] == 0.8
    assert metrics["symbol_regime_fit"] == 0.9
    assert metrics["mtf_alignment"] == 0.6
    assert metrics["strategy_confidence"] == 0.8
    assert "expected_r_net_of_costs" in metrics


def test_untradeable_symbol_has_terminal_reason_and_runs_no_strategy() -> None:
    result = pipeline(plugin("vwap")).run(
        (context(tradeable=False),),
        preconditions=normal(),
        evaluated_at=NOW,
        live_quotes={},
    )

    assert result.candidates == ()
    assert any(
        decision.stage is DecisionStage.TRADEABILITY and decision.reason_code == "MIN_TRADED_VALUE"
        for decision in result.decisions
    )


class BlockingReviewer:
    def review(self, candidate: object) -> LlmVerdict:
        del candidate
        return LlmVerdict.BLOCK


@pytest.mark.parametrize(
    ("mode", "expected"),
    ((LlmReviewMode.SHADOW, 1), (LlmReviewMode.ENFORCED_VETO, 0)),
)
def test_llm_block_is_shadowed_or_enforced_by_explicit_mode(
    mode: LlmReviewMode, expected: int
) -> None:
    result = pipeline(plugin("vwap"), llm_mode=mode, reviewer=BlockingReviewer()).run(
        (context(),),
        preconditions=normal(),
        evaluated_at=NOW,
        live_quotes={(Market.NSE, "RELIANCE"): 100},
    )
    assert len(result.allocations) == expected


def test_allocation_enforces_sector_and_correlation_at_batch_level() -> None:
    result = pipeline(
        plugin("vwap"),
        allocation_policy=policy(max_sector_positions=1),
    ).run(
        (
            context("SBIN", sector="BANK"),
            context("HDFCBANK", sector="BANK"),
            context("RELIANCE", sector="ENERGY"),
        ),
        preconditions=normal(),
        evaluated_at=NOW,
        live_quotes={
            (Market.NSE, "SBIN"): 100,
            (Market.NSE, "HDFCBANK"): 100,
            (Market.NSE, "RELIANCE"): 100,
        },
        correlations={("HDFCBANK", "RELIANCE"): 0.8},
    )

    assert [item.candidate.candidate.symbol for item in result.allocations] == ["HDFCBANK"]
    reasons = {decision.reason_code for decision in result.decisions}
    assert "SECTOR_CONCENTRATION" in reasons
    assert "CORRELATION_LIMIT" in reasons


def test_entry_drift_rejects_candidate_after_portfolio_selection() -> None:
    result = pipeline(plugin("vwap")).run(
        (context(),),
        preconditions=normal(),
        evaluated_at=NOW,
        live_quotes={(Market.NSE, "RELIANCE"): 101},
    )
    assert result.allocations == ()
    assert "ENTRY_DRIFT_EXCEEDED" in {decision.reason_code for decision in result.decisions}


class RecordingPositionManager:
    def __init__(self) -> None:
        self.called = False

    def manage(self, *, cycle_id: str, evaluated_at: datetime) -> tuple[Decision, ...]:
        self.called = True
        return (
            Decision.create(
                cycle_id=cycle_id,
                market=Market.NSE,
                symbol="EXISTING",
                timeframe=None,
                stage=DecisionStage.POSITION_MANAGEMENT,
                status=DecisionStatus.PASSED,
                reason_code="POSITION_MANAGED",
                occurred_at=evaluated_at,
            ),
        )


def test_position_management_runs_when_entries_are_killed() -> None:
    manager = RecordingPositionManager()
    result = pipeline(plugin("vwap"), position_manager=manager).run(
        (context(),),
        preconditions=CyclePreconditions(False, False, False, False),
        evaluated_at=NOW,
        live_quotes={},
    )
    assert manager.called
    assert result.mode.value == "EXITS_ONLY"
    assert any(decision.reason_code == "POSITION_MANAGED" for decision in result.decisions)


def test_constructed_batch_flows_through_risk_into_paper_orders() -> None:
    strategy = plugin("vwap")
    registry, catalog = approved_registry(strategy)
    ledger = InMemoryDecisionLedger()
    staged = StagedDecisionPipeline(
        registry=registry,
        strategies=catalog,
        ledger=ledger,
        allocation_policy=policy(),
    )
    result = staged.run(
        (context(),),
        preconditions=normal(),
        evaluated_at=NOW,
        live_quotes={(Market.NSE, "RELIANCE"): 100},
    )
    executor = PaperBatchExecutor(
        registry=registry,
        risk=RiskEngine(RiskLimits(20_000, 20_000, 50_000, 50_000, 2_000, 5)),
        execution=PaperExecutionEngine(ExecutionPolicy(1, 1)),
        ledger=ledger,
    )
    batch = executor.execute(
        result,
        account_id="paper-nse",
        portfolio=PortfolioSnapshot("snapshot", "paper-nse", 100_000, 0, (), NOW),
        evaluated_at=NOW,
    )

    assert len(batch.risk_decisions) == 1
    assert len(batch.receipts) == 1
    reasons = {decision.reason_code for decision in ledger.for_cycle(result.cycle_id)}
    assert "RISK_APPROVED" in reasons
    assert "PAPER_ORDER_CREATED" in reasons
