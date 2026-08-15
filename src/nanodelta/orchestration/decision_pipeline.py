"""Three-plane strategy pipeline: generate, score, then construct a portfolio batch."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from nanodelta.contracts import AdvisoryAction, Market, stable_id, utc
from nanodelta.decisions import (
    Decision,
    DecisionLedger,
    DecisionStage,
    DecisionStatus,
)
from nanodelta.strategies import (
    DeterministicCandidate,
    StrategyContext,
    StrategyRegistry,
    StrategyRuntimeCatalog,
)


class CycleMode(StrEnum):
    NORMAL = "NORMAL"
    EXITS_ONLY = "EXITS_ONLY"


@dataclass(frozen=True)
class CyclePreconditions:
    entry_kill_switch_clear: bool
    daily_loss_capacity: bool
    entry_session_open: bool
    capital_available: bool

    @property
    def mode(self) -> CycleMode:
        return (
            CycleMode.NORMAL
            if all(
                (
                    self.entry_kill_switch_clear,
                    self.daily_loss_capacity,
                    self.entry_session_open,
                    self.capital_available,
                )
            )
            else CycleMode.EXITS_ONLY
        )

    @property
    def reason(self) -> str:
        if not self.entry_kill_switch_clear:
            return "ENTRY_KILL_SWITCH_ACTIVE"
        if not self.daily_loss_capacity:
            return "DAILY_LOSS_LIMIT_REACHED"
        if not self.entry_session_open:
            return "ENTRY_SESSION_CLOSED"
        if not self.capital_available:
            return "INSUFFICIENT_CAPITAL"
        return "ENTRY_PRECONDITIONS_PASSED"


@dataclass(frozen=True)
class ScoreBreakdown:
    strategy_confidence: float
    market_regime_fit: float
    sector_regime_fit: float
    symbol_regime_fit: float
    mtf_alignment: float
    historical_expectancy_r: float
    ml_tilt_r: float
    estimated_cost_r: float
    expected_r_net_of_costs: float


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: DeterministicCandidate
    score: ScoreBreakdown


class LlmReviewMode(StrEnum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    ENFORCED_VETO = "ENFORCED_VETO"


class LlmVerdict(StrEnum):
    PASS = "PASS"
    BLOCK = "BLOCK"
    ABSTAIN = "ABSTAIN"
    UNAVAILABLE = "UNAVAILABLE"


class CandidateReviewer(Protocol):
    def review(self, candidate: ScoredCandidate) -> LlmVerdict: ...


@dataclass(frozen=True)
class AllocationPolicy:
    equity: float
    risk_fraction_per_trade: float
    max_order_notional: float
    max_total_new_notional: float
    max_positions: int
    max_sector_positions: int
    max_pairwise_correlation: float = 0.70
    minimum_expected_r: float = 0.0
    maximum_entry_drift_fraction: float = 0.003
    minimum_reward_risk: float = 1.0

    def __post_init__(self) -> None:
        positives = (
            self.equity,
            self.risk_fraction_per_trade,
            self.max_order_notional,
            self.max_total_new_notional,
            self.max_positions,
            self.max_sector_positions,
        )
        if any(not math.isfinite(float(value)) or value <= 0 for value in positives):
            raise ValueError("allocation limits must be finite and positive")
        if not 0 <= self.max_pairwise_correlation <= 1:
            raise ValueError("correlation limit must be in [0, 1]")
        if self.maximum_entry_drift_fraction < 0 or self.minimum_reward_risk <= 0:
            raise ValueError("entry revalidation settings are invalid")


@dataclass(frozen=True)
class PortfolioAllocation:
    candidate: ScoredCandidate
    quantity: float
    expected_entry_price: float
    stop_price: float
    target_price: float

    @property
    def notional(self) -> float:
        return self.quantity * self.expected_entry_price


@dataclass(frozen=True)
class PipelineResult:
    cycle_id: str
    mode: CycleMode
    candidates: tuple[DeterministicCandidate, ...]
    scored: tuple[ScoredCandidate, ...]
    allocations: tuple[PortfolioAllocation, ...]
    decisions: tuple[Decision, ...]


class PositionManager(Protocol):
    """Runs before entry preconditions; implementations own stops/targets/exits."""

    def manage(self, *, cycle_id: str, evaluated_at: datetime) -> tuple[Decision, ...]: ...


class StagedDecisionPipeline:
    def __init__(
        self,
        *,
        registry: StrategyRegistry,
        strategies: StrategyRuntimeCatalog,
        ledger: DecisionLedger,
        allocation_policy: AllocationPolicy,
        llm_mode: LlmReviewMode = LlmReviewMode.OFF,
        reviewer: CandidateReviewer | None = None,
        position_manager: PositionManager | None = None,
    ) -> None:
        if llm_mode is not LlmReviewMode.OFF and reviewer is None:
            raise ValueError("an LLM reviewer is required when review is enabled")
        self._registry = registry
        self._strategies = strategies
        self._ledger = ledger
        self._policy = allocation_policy
        self._llm_mode = llm_mode
        self._reviewer = reviewer
        self._position_manager = position_manager

    def run(
        self,
        contexts: tuple[StrategyContext, ...],
        *,
        preconditions: CyclePreconditions,
        evaluated_at: datetime,
        live_quotes: Mapping[tuple[Market, str], float],
        existing_symbols: frozenset[tuple[Market, str]] = frozenset(),
        correlations: Mapping[tuple[str, str], float] | None = None,
    ) -> PipelineResult:
        evaluated_at = utc(evaluated_at, "evaluated_at")
        cycle_id = stable_id(
            "decision-cycle",
            evaluated_at.isoformat(),
            tuple(
                sorted(
                    (
                        context.market.value,
                        context.symbol,
                        context.timeframe,
                        context.trade_horizon,
                        context.feature_set_version,
                        *context.gold_snapshot_ids,
                    )
                    for context in contexts
                )
            ),
        )
        emitted: list[Decision] = []
        self._manage_positions(cycle_id, evaluated_at, emitted)
        if preconditions.mode is CycleMode.EXITS_ONLY:
            for context in contexts:
                self._emit(
                    emitted,
                    context,
                    cycle_id,
                    DecisionStage.GLOBAL,
                    DecisionStatus.REJECTED,
                    preconditions.reason,
                    evaluated_at,
                )
            return self._result(cycle_id, CycleMode.EXITS_ONLY, (), (), (), emitted)

        candidates = self._generate(contexts, cycle_id, evaluated_at, emitted)
        scored = self._score(candidates, cycle_id, evaluated_at, emitted)
        reviewed = self._review(scored, cycle_id, evaluated_at, emitted)
        allocations = self._allocate(
            reviewed,
            cycle_id,
            evaluated_at,
            existing_symbols,
            correlations or {},
            emitted,
        )
        valid = self._revalidate(allocations, live_quotes, cycle_id, evaluated_at, emitted)
        return self._result(
            cycle_id,
            CycleMode.NORMAL,
            tuple(candidates),
            tuple(scored),
            tuple(valid),
            emitted,
        )

    def _generate(
        self,
        contexts: tuple[StrategyContext, ...],
        cycle_id: str,
        at: datetime,
        emitted: list[Decision],
    ) -> list[DeterministicCandidate]:
        candidates: list[DeterministicCandidate] = []
        for context in contexts:
            readiness_reason = self._readiness_reason(context)
            if readiness_reason is not None:
                self._emit(
                    emitted,
                    context,
                    cycle_id,
                    DecisionStage.DATA_READINESS,
                    DecisionStatus.REJECTED,
                    readiness_reason,
                    at,
                )
                continue
            self._emit(
                emitted,
                context,
                cycle_id,
                DecisionStage.DATA_READINESS,
                DecisionStatus.PASSED,
                "DATA_READY",
                at,
            )
            if not context.tradeable:
                self._emit(
                    emitted,
                    context,
                    cycle_id,
                    DecisionStage.TRADEABILITY,
                    DecisionStatus.REJECTED,
                    context.tradeability_reason,
                    at,
                )
                continue
            self._emit(
                emitted,
                context,
                cycle_id,
                DecisionStage.TRADEABILITY,
                DecisionStatus.PASSED,
                "SYMBOL_TRADEABLE",
                at,
            )
            definitions = self._registry.eligible(
                market=context.market,
                timeframe=context.timeframe,
                trade_horizon=context.trade_horizon,
                feature_set_version=context.feature_set_version,
                at=at,
            )
            if not definitions:
                self._emit(
                    emitted,
                    context,
                    cycle_id,
                    DecisionStage.STRATEGY_ELIGIBILITY,
                    DecisionStatus.REJECTED,
                    "NO_APPROVED_STRATEGY",
                    at,
                )
                continue
            for definition in definitions:
                plugin = self._strategies.require(definition.identity)
                compatible, reason = plugin.compatibility(context)
                if not compatible:
                    self._emit(
                        emitted,
                        context,
                        cycle_id,
                        DecisionStage.STRATEGY_ELIGIBILITY,
                        DecisionStatus.REJECTED,
                        reason,
                        at,
                        strategy_key=definition.identity.key,
                    )
                    continue
                approval = self._registry.require_approval(definition.identity, at=at)
                self._emit(
                    emitted,
                    context,
                    cycle_id,
                    DecisionStage.STRATEGY_ELIGIBILITY,
                    DecisionStatus.PASSED,
                    "STRATEGY_ELIGIBLE",
                    at,
                    strategy_key=definition.identity.key,
                )
                signal = plugin.generate(context)
                if signal is None:
                    self._emit(
                        emitted,
                        context,
                        cycle_id,
                        DecisionStage.SIGNAL,
                        DecisionStatus.REJECTED,
                        "NO_TRIGGER",
                        at,
                        strategy_key=definition.identity.key,
                    )
                    continue
                candidate = DeterministicCandidate.create(
                    definition, approval.approval_id, context, signal
                )
                candidates.append(candidate)
                self._emit(
                    emitted,
                    context,
                    cycle_id,
                    DecisionStage.SIGNAL,
                    DecisionStatus.PASSED,
                    "SIGNAL_GENERATED",
                    at,
                    candidate_id=candidate.candidate_id,
                    strategy_key=definition.identity.key,
                )
        return candidates

    def _score(
        self,
        candidates: list[DeterministicCandidate],
        cycle_id: str,
        at: datetime,
        emitted: list[Decision],
    ) -> list[ScoredCandidate]:
        result = []
        for candidate in candidates:
            signal = candidate.signal
            regime = candidate.regime
            expected_r = (
                signal.confidence
                * regime.market_fit
                * regime.sector_fit
                * regime.symbol_fit
                * regime.mtf_alignment
                + signal.historical_expectancy_r
                + signal.ml_tilt_r
                - signal.estimated_cost_r
            )
            score = ScoreBreakdown(
                signal.confidence,
                regime.market_fit,
                regime.sector_fit,
                regime.symbol_fit,
                regime.mtf_alignment,
                signal.historical_expectancy_r,
                signal.ml_tilt_r,
                signal.estimated_cost_r,
                expected_r,
            )
            item = ScoredCandidate(candidate, score)
            status = (
                DecisionStatus.PASSED
                if expected_r > self._policy.minimum_expected_r
                else DecisionStatus.REJECTED
            )
            reason = (
                "POSITIVE_EXPECTED_R"
                if status is DecisionStatus.PASSED
                else "NON_POSITIVE_EXPECTED_R"
            )
            self._emit_candidate(emitted, item, cycle_id, DecisionStage.SCORING, status, reason, at)
            if status is DecisionStatus.PASSED:
                result.append(item)
        return sorted(
            result,
            key=lambda item: (
                -item.score.expected_r_net_of_costs,
                item.candidate.identity.key,
                item.candidate.symbol,
                item.candidate.candidate_id,
            ),
        )

    def _review(
        self,
        candidates: list[ScoredCandidate],
        cycle_id: str,
        at: datetime,
        emitted: list[Decision],
    ) -> list[ScoredCandidate]:
        if self._llm_mode is LlmReviewMode.OFF:
            for item in candidates:
                self._emit_candidate(
                    emitted,
                    item,
                    cycle_id,
                    DecisionStage.LLM_REVIEW,
                    DecisionStatus.SKIPPED,
                    "LLM_OFF",
                    at,
                )
            return candidates
        assert self._reviewer is not None
        result = []
        for item in candidates:
            try:
                verdict = self._reviewer.review(item)
            except Exception:
                verdict = LlmVerdict.UNAVAILABLE
            blocked = self._llm_mode is LlmReviewMode.ENFORCED_VETO and verdict is LlmVerdict.BLOCK
            self._emit_candidate(
                emitted,
                item,
                cycle_id,
                DecisionStage.LLM_REVIEW,
                DecisionStatus.REJECTED if blocked else DecisionStatus.PASSED,
                f"LLM_{verdict.value}",
                at,
            )
            if not blocked:
                result.append(item)
        return result

    def _allocate(
        self,
        candidates: list[ScoredCandidate],
        cycle_id: str,
        at: datetime,
        existing_symbols: frozenset[tuple[Market, str]],
        correlations: Mapping[tuple[str, str], float],
        emitted: list[Decision],
    ) -> list[PortfolioAllocation]:
        allocations: list[PortfolioAllocation] = []
        sectors: dict[str, int] = {}
        total_notional = 0.0
        risk_cash = self._policy.equity * self._policy.risk_fraction_per_trade
        for item in candidates:
            candidate = item.candidate
            if (candidate.identity.market, candidate.symbol) in existing_symbols:
                self._portfolio_reject(emitted, item, cycle_id, at, "EXISTING_POSITION")
                continue
            if any(
                allocation.candidate.candidate.symbol == candidate.symbol
                for allocation in allocations
            ):
                self._portfolio_reject(emitted, item, cycle_id, at, "SYMBOL_ALREADY_SELECTED")
                continue
            if len(allocations) >= self._policy.max_positions:
                self._portfolio_reject(emitted, item, cycle_id, at, "POSITION_CAPACITY_REACHED")
                continue
            sector = candidate.sector or "UNCLASSIFIED"
            if sectors.get(sector, 0) >= self._policy.max_sector_positions:
                self._portfolio_reject(emitted, item, cycle_id, at, "SECTOR_CONCENTRATION")
                continue
            if self._correlated(candidate.symbol, allocations, correlations):
                self._portfolio_reject(emitted, item, cycle_id, at, "CORRELATION_LIMIT")
                continue
            stop_distance = abs(candidate.signal.reference_price - candidate.signal.stop_price)
            quantity = min(
                risk_cash / stop_distance,
                self._policy.max_order_notional / candidate.signal.reference_price,
            )
            notional = quantity * candidate.signal.reference_price
            if total_notional + notional > self._policy.max_total_new_notional:
                self._portfolio_reject(emitted, item, cycle_id, at, "INSUFFICIENT_BATCH_CAPITAL")
                continue
            allocation = PortfolioAllocation(
                item,
                quantity,
                candidate.signal.reference_price,
                candidate.signal.stop_price,
                candidate.signal.target_price,
            )
            allocations.append(allocation)
            total_notional += notional
            sectors[sector] = sectors.get(sector, 0) + 1
            self._emit_candidate(
                emitted,
                item,
                cycle_id,
                DecisionStage.PORTFOLIO_CONSTRUCTION,
                DecisionStatus.PASSED,
                "PORTFOLIO_SELECTED",
                at,
                extra_metrics=(("quantity", quantity), ("notional", notional)),
            )
        return allocations

    def _revalidate(
        self,
        allocations: list[PortfolioAllocation],
        quotes: Mapping[tuple[Market, str], float],
        cycle_id: str,
        at: datetime,
        emitted: list[Decision],
    ) -> list[PortfolioAllocation]:
        valid = []
        for allocation in allocations:
            candidate = allocation.candidate.candidate
            quote = quotes.get((candidate.identity.market, candidate.symbol))
            if quote is None or not math.isfinite(quote) or quote <= 0:
                self._entry_reject(emitted, allocation, cycle_id, at, "LIVE_QUOTE_UNAVAILABLE")
                continue
            drift = abs(quote - candidate.signal.reference_price) / candidate.signal.reference_price
            if drift > self._policy.maximum_entry_drift_fraction:
                self._entry_reject(emitted, allocation, cycle_id, at, "ENTRY_DRIFT_EXCEEDED", drift)
                continue
            risk = abs(quote - candidate.signal.stop_price)
            reward = abs(candidate.signal.target_price - quote)
            reward_risk = reward / risk if risk > 0 else 0.0
            geometry_valid = (
                candidate.signal.stop_price < quote < candidate.signal.target_price
                if candidate.signal.action is AdvisoryAction.BUY
                else candidate.signal.target_price < quote < candidate.signal.stop_price
            )
            if not geometry_valid or reward_risk < self._policy.minimum_reward_risk:
                self._entry_reject(
                    emitted, allocation, cycle_id, at, "RR_DEGRADED_AT_ENTRY", reward_risk
                )
                continue
            valid.append(
                PortfolioAllocation(
                    allocation.candidate,
                    allocation.quantity,
                    quote,
                    allocation.stop_price,
                    allocation.target_price,
                )
            )
            self._emit_candidate(
                emitted,
                allocation.candidate,
                cycle_id,
                DecisionStage.ENTRY_REVALIDATION,
                DecisionStatus.PASSED,
                "ENTRY_REVALIDATED",
                at,
                extra_metrics=(("entry_drift", drift), ("reward_risk", reward_risk)),
            )
        return valid

    @staticmethod
    def _readiness_reason(context: StrategyContext) -> str | None:
        checks = (
            (context.settled, "NO_SETTLED_BAR"),
            (context.complete, "MISSING_BARS"),
            (context.adjusted, "CORP_ACTION_SUSPECT"),
            (context.fresh, "STALE_QUOTE"),
            (context.warm, "INDICATOR_WARMUP"),
        )
        return next((reason for passed, reason in checks if not passed), None)

    def _manage_positions(self, cycle_id: str, at: datetime, emitted: list[Decision]) -> None:
        if self._position_manager is None:
            return
        for decision in self._position_manager.manage(cycle_id=cycle_id, evaluated_at=at):
            self._ledger.append(decision)
            emitted.append(decision)

    def _correlated(
        self,
        symbol: str,
        allocations: list[PortfolioAllocation],
        correlations: Mapping[tuple[str, str], float],
    ) -> bool:
        for allocation in allocations:
            other = allocation.candidate.candidate.symbol
            value = correlations.get((symbol, other), correlations.get((other, symbol), 0.0))
            if abs(value) > self._policy.max_pairwise_correlation:
                return True
        return False

    def _portfolio_reject(
        self,
        emitted: list[Decision],
        item: ScoredCandidate,
        cycle_id: str,
        at: datetime,
        reason: str,
    ) -> None:
        self._emit_candidate(
            emitted,
            item,
            cycle_id,
            DecisionStage.PORTFOLIO_CONSTRUCTION,
            DecisionStatus.REJECTED,
            reason,
            at,
        )

    def _entry_reject(
        self,
        emitted: list[Decision],
        allocation: PortfolioAllocation,
        cycle_id: str,
        at: datetime,
        reason: str,
        observed: float | None = None,
    ) -> None:
        metrics = (("observed", observed),) if observed is not None else ()
        self._emit_candidate(
            emitted,
            allocation.candidate,
            cycle_id,
            DecisionStage.ENTRY_REVALIDATION,
            DecisionStatus.REJECTED,
            reason,
            at,
            extra_metrics=metrics,
        )

    def _emit_candidate(
        self,
        emitted: list[Decision],
        item: ScoredCandidate,
        cycle_id: str,
        stage: DecisionStage,
        status: DecisionStatus,
        reason: str,
        at: datetime,
        *,
        extra_metrics: tuple[tuple[str, float], ...] = (),
    ) -> None:
        candidate = item.candidate
        metrics = (
            ("expected_r_net_of_costs", item.score.expected_r_net_of_costs),
            *extra_metrics,
        )
        decision = Decision.create(
            cycle_id=cycle_id,
            market=candidate.identity.market,
            symbol=candidate.symbol,
            timeframe=candidate.identity.timeframe,
            stage=stage,
            status=status,
            reason_code=reason,
            occurred_at=at,
            candidate_id=candidate.candidate_id,
            strategy_key=candidate.identity.key,
            metrics=metrics,
        )
        self._ledger.append(decision)
        emitted.append(decision)

    def _emit(
        self,
        emitted: list[Decision],
        context: StrategyContext,
        cycle_id: str,
        stage: DecisionStage,
        status: DecisionStatus,
        reason: str,
        at: datetime,
        *,
        candidate_id: str | None = None,
        strategy_key: str | None = None,
    ) -> None:
        decision = Decision.create(
            cycle_id=cycle_id,
            market=context.market,
            symbol=context.symbol,
            timeframe=context.timeframe,
            stage=stage,
            status=status,
            reason_code=reason,
            occurred_at=at,
            candidate_id=candidate_id,
            strategy_key=strategy_key,
        )
        self._ledger.append(decision)
        emitted.append(decision)

    @staticmethod
    def _result(
        cycle_id: str,
        mode: CycleMode,
        candidates: tuple[DeterministicCandidate, ...],
        scored: tuple[ScoredCandidate, ...],
        allocations: tuple[PortfolioAllocation, ...],
        decisions: list[Decision],
    ) -> PipelineResult:
        return PipelineResult(cycle_id, mode, candidates, scored, allocations, tuple(decisions))
