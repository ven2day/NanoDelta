"""Settled-candle Forex cycle built from the existing shared strategy core.

This is an ingestion/orchestration adapter, not a second strategy engine. It creates
the same FeatureSnapshot, RegisteredStrategySignal, and AggregatedCandidate objects
as NSE. Only independently validated PAPER_APPROVED Forex grains can create local
paper decisions; this module never submits OANDA broker orders.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime
from time import perf_counter
from typing import Any, cast

import pandas as pd

from src.agents.prediction import PredictionAgent
from src.agents.signal_validation import signal_validation_node
from src.agents.state import TradingState
from src.backtesting.production_strategy_registration import (
    ensure_production_strategy_placeholders,
)
from src.backtesting.strategy_eligibility import (
    EligibilityEnvironment,
    StrategyEligibilityRegistry,
)
from src.core.aggregation.consolidation import (
    AggregatedCandidate,
    FeatureSnapshot,
    aggregate_strategy_signals,
    eligible_strategy_types,
    evaluate_registered_strategies,
)
from src.core.candidates import SignalEngine, SignalType, TradeHorizon
from src.core.decisions import (
    DecisionStatus,
    SchemaBoundDecisionRepository,
    decision_record_from_payload,
    persist_decision_records,
)
from src.core.features import SchemaBoundFeatureRepository, persist_feature_snapshots
from src.core.indicators import Timeframe, calculate_indicators
from src.core.ml import MLPolicy, ModelGrain, ModelRegistry, evaluate_ml_policy
from src.core.models import CanonicalCandle, Instrument, PriceSide
from src.core.persistence import SchemaBoundTradingRepository
from src.core.qwen import QwenReviewPolicy
from src.core.strategies.config import ProductionStrategyConfig
from src.markets.forex.broker.oanda import OandaMarketDataProvider
from src.markets.forex.eligibility import ForexRegimeClassifier
from src.markets.forex.persistence import forex_record_id
from src.markets.forex.risk import (
    ForexRiskLimits,
    evaluate_forex_pretrade,
    quote_to_home_rate,
    size_forex_position,
)
from src.markets.forex.runtime.lifecycle import (
    ForexDecisionLifecycle,
    ForexDecisionStage,
    candidate_identity,
    classify_instrument_conflicts,
    update_candidate_state,
)
from src.markets.forex.runtime.paper_positions import (
    create_local_paper_position,
    persist_lifecycle_record,
)


@dataclass
class ForexCycleMetrics:
    instruments_requested: int = 0
    instruments_scanned: int = 0
    candles_retrieved: int = 0
    missing_or_stale: int = 0
    feature_snapshots: int = 0
    strategy_evaluations: int = 0
    technical_buy_candidates: int = 0
    technical_sell_candidates: int = 0
    consolidated_candidates: int = 0
    conflicts: int = 0
    conflicts_low: int = 0
    conflicts_high: int = 0
    conflicts_unresolved: int = 0
    eligibility_passed: int = 0
    eligibility_blocked: int = 0
    regime_blocked: int = 0
    multi_timeframe_blocked: int = 0
    entry_quality_passed: int = 0
    confidence_reduced: int = 0
    ml_evaluated: int = 0
    ml_qualified: int = 0
    ml_abstained: int = 0
    qwen_reviewed: int = 0
    qwen_required: int = 0
    qwen_skipped_clear: int = 0
    qwen_deferred: int = 0
    qwen_cache_hits: int = 0
    qwen_external_calls: int = 0
    qwen_input_tokens: int = 0
    qwen_output_tokens: int = 0
    risk_approved: int = 0
    risk_blocked: int = 0
    shadow_decisions: int = 0
    final_buy: int = 0
    final_sell: int = 0
    paper_orders: int = 0
    persistence_writes: int = 0
    feature_duration_ms: float = 0.0
    strategy_duration_ms: float = 0.0
    aggregation_duration_ms: float = 0.0
    risk_duration_ms: float = 0.0
    ml_duration_ms: float = 0.0
    qwen_duration_ms: float = 0.0
    persistence_duration_ms: float = 0.0
    market_data_duration_ms: float = 0.0
    total_duration_ms: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ForexCycleResult:
    metrics: ForexCycleMetrics
    snapshots: tuple[FeatureSnapshot, ...]
    candidates: tuple[AggregatedCandidate, ...]
    conflicts: tuple[dict[str, Any], ...] = ()
    technical_candidates: tuple[dict[str, Any], ...] = ()


def candles_to_frame(candles: list[CanonicalCandle]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    ordered = sorted(candles, key=lambda item: item.timestamp)
    frame = pd.DataFrame(
        {
            "open": [item.open for item in ordered],
            "high": [item.high for item in ordered],
            "low": [item.low for item in ordered],
            "close": [item.close for item in ordered],
            "volume": [item.volume for item in ordered],
        },
        index=pd.DatetimeIndex([item.timestamp for item in ordered], name="timestamp"),
    )
    return frame[~frame.index.duplicated(keep="last")]


class ForexSettledCandleCycle:
    def __init__(
        self,
        *,
        provider: OandaMarketDataProvider,
        signal_engine: SignalEngine,
        strategy_config: ProductionStrategyConfig,
        eligibility_registry: StrategyEligibilityRegistry,
        risk_limits: ForexRiskLimits,
        candle_store: Any | None = None,
        trading_repository: SchemaBoundTradingRepository | None = None,
        feature_repository: SchemaBoundFeatureRepository | None = None,
        decision_repository: SchemaBoundDecisionRepository | None = None,
        prediction_agent: PredictionAgent | None = None,
        model_registry: ModelRegistry | None = None,
        settings: Any | None = None,
    ) -> None:
        if strategy_config.market != "FOREX":
            raise ValueError("Forex cycle requires a FOREX ProductionStrategyConfig")
        self.provider = provider
        self.signal_engine = signal_engine
        self.strategy_config = strategy_config
        self.eligibility_registry = eligibility_registry
        self.risk_limits = risk_limits
        self.candle_store = candle_store
        self.trading_repository = trading_repository
        self.feature_repository = feature_repository
        self.decision_repository = decision_repository
        self.prediction_agent = prediction_agent
        self.model_registry = model_registry
        self.settings = settings
        # PAPER-ONLY: lift the "must be PAPER_APPROVED" eligibility gate so SHADOW
        # strategies can produce paper decisions. Never affects live routing.
        self.allow_unvalidated_paper = bool(
            getattr(settings, "forex_allow_unvalidated_paper", False)
        )
        self.regime_classifier = ForexRegimeClassifier.from_settings(settings)
        self._last_processed: dict[tuple[str, str], str] = {}
        self._history_frames: dict[tuple[str, str], pd.DataFrame] = {}
        self._candidate_state: dict[tuple[str, str], AggregatedCandidate] = {}
        ensure_production_strategy_placeholders(eligibility_registry, strategy_config)
        if trading_repository is not None:
            for record in eligibility_registry.load_all():
                if record.market.upper() != "FOREX":
                    continue
                trading_repository.persist_strategy_eligibility(
                    identity="|".join(record.identity), payload=record.to_dict()
                )

    async def run(
        self,
        *,
        timeframes: tuple[str, ...] = ("15m", "30m", "1h", "4h"),
        candle_count: int = 260,
        trade_horizon: TradeHorizon = TradeHorizon.SWING,
    ) -> ForexCycleResult:
        started = perf_counter()
        metrics = ForexCycleMetrics()
        instruments = await self.provider.list_instruments()
        metrics.instruments_requested = len(instruments)
        requested = [
            (instrument, timeframe)
            for instrument in instruments
            for timeframe in timeframes
            if any(
                self.strategy_config.eligible(name, timeframe)
                for name in self.strategy_config.timeframe_map
            )
        ]
        semaphore = asyncio.Semaphore(4)

        async def fetch(
            pair: tuple[Instrument, str],
        ) -> tuple[Instrument, str, list[CanonicalCandle]]:
            instrument, timeframe = pair
            request_count = (
                2 if (instrument.symbol, timeframe) in self._history_frames else candle_count
            )
            async with semaphore:
                candles = await self.provider.get_candles(
                    instrument.symbol, timeframe, count=request_count, complete_only=True
                )
            return instrument, timeframe, candles

        market_started = perf_counter()
        fetched = await asyncio.gather(*(fetch(pair) for pair in requested), return_exceptions=True)
        metrics.market_data_duration_ms = (perf_counter() - market_started) * 1000
        snapshots: list[FeatureSnapshot] = []
        registered = []
        snapshot_context: dict[str, tuple[Instrument, Any]] = {}
        scanned_instruments: set[str] = set()
        for item in fetched:
            if isinstance(item, BaseException):
                metrics.missing_or_stale += 1
                metrics.errors.append(type(item).__name__)
                continue
            instrument, timeframe, candles = item
            metrics.candles_retrieved += len(candles)
            if not candles:
                metrics.missing_or_stale += 1
                continue
            settled = candles[-1]
            token = settled.timestamp.isoformat()
            key = (instrument.symbol, timeframe)
            if self._last_processed.get(key) == token:
                continue
            self._last_processed[key] = token
            incremental = candles_to_frame(candles)
            previous = self._history_frames.get(key)
            frame = incremental if previous is None else pd.concat([previous, incremental])
            frame = frame[~frame.index.duplicated(keep="last")].sort_index().tail(candle_count)
            self._history_frames[key] = frame
            if len(frame) < 50:
                metrics.missing_or_stale += 1
                continue
            if self.candle_store is not None:
                await asyncio.to_thread(
                    self.candle_store.upsert_frame,
                    instrument.symbol,
                    timeframe,
                    frame,
                    source="oanda",
                    market="FOREX",
                    provider="OANDA",
                    volume_type="OANDA_TICK_COUNT",
                )
            feature_started = perf_counter()
            indicator = calculate_indicators(
                frame,
                instrument.symbol,
                timeframe=Timeframe(timeframe),
            )
            snapshot = FeatureSnapshot.create(
                indicator,
                settled_candle_timestamp=token,
                market="FOREX",
                provider="OANDA",
                volume_type="OANDA_TICK_COUNT",
            )
            metrics.feature_duration_ms += (perf_counter() - feature_started) * 1000
            metrics.feature_snapshots += 1
            scanned_instruments.add(instrument.symbol)
            snapshots.append(snapshot)
            snapshot_context[snapshot.snapshot_id] = (instrument, indicator)
            metrics.strategy_evaluations += len(
                eligible_strategy_types(self.signal_engine, snapshot.timeframe)
            )
            strategy_started = perf_counter()
            regime = self.regime_classifier.classify(indicator)
            outputs = evaluate_registered_strategies(
                self.signal_engine,
                snapshot,
                self.eligibility_registry,
                trade_horizon=trade_horizon,
                regime=regime,
                execution_mode="local_paper",
                eligibility_environment=EligibilityEnvironment.PAPER,
            )
            metrics.strategy_duration_ms += (perf_counter() - strategy_started) * 1000
            registered.extend(outputs)
            if self.trading_repository is not None:
                for output in outputs:
                    signal_id = forex_record_id(
                        "signal",
                        output.feature_snapshot_id,
                        output.strategy_name,
                        output.signal.signal_type.value,
                    )
                    await asyncio.to_thread(
                        self.trading_repository.persist_signal,
                        signal_id=signal_id,
                        symbol=output.signal.symbol,
                        timeframe=output.signal.timeframe.value,
                        side=output.signal.signal_type.value,
                        strategy=output.strategy_name,
                        payload=output.to_dict(),
                        created_at=output.settled_candle_timestamp,
                    )
                    metrics.persistence_writes += 1
        metrics.persistence_writes += await asyncio.to_thread(
            persist_feature_snapshots,
            self.feature_repository,
            snapshots,
        )
        metrics.instruments_scanned = len(scanned_instruments)
        metrics.technical_buy_candidates = sum(
            item.signal.signal_type is SignalType.BUY for item in registered
        )
        metrics.technical_sell_candidates = sum(
            item.signal.signal_type is SignalType.SELL for item in registered
        )
        aggregation_started = perf_counter()
        candidates = aggregate_strategy_signals(registered)
        metrics.aggregation_duration_ms = (perf_counter() - aggregation_started) * 1000
        metrics.consolidated_candidates = len(candidates)
        metrics.conflicts = sum(item.conflict_level != "NONE" for item in candidates)
        metrics.conflicts_low = sum(item.conflict_level == "LOW" for item in candidates)
        metrics.conflicts_high = sum(item.conflict_level == "HIGH" for item in candidates)
        metrics.conflicts_unresolved = sum(
            item.conflict_resolution == "UNRESOLVED_BLOCK" for item in candidates
        )
        metrics.eligibility_passed = sum(item.pipeline_eligible for item in candidates)
        metrics.eligibility_blocked = len(candidates) - metrics.eligibility_passed
        lifecycle_by_key = {
            candidate_identity(candidate): ForexDecisionLifecycle.from_candidate(candidate)
            for candidate in candidates
        }
        candidate_context = update_candidate_state(
            self._candidate_state,
            changed_grains=(
                (snapshot.symbol, snapshot.timeframe.value) for snapshot in snapshots
            ),
            current_candidates=candidates,
        )
        instrument_resolutions = classify_instrument_conflicts(candidate_context)
        for candidate in candidates:
            candidate_key = candidate_identity(candidate)
            lifecycle_by_key[candidate_key].multi_timeframe = instrument_resolutions[
                candidate_key
            ].to_dict()
        metrics.multi_timeframe_blocked = sum(
            1
            for candidate in candidates
            if candidate.pipeline_eligible
            and instrument_resolutions[candidate_identity(candidate)].conflicted
        )
        for candidate in candidates:
            evidence = (*candidate.supporting_signals, *candidate.opposing_signals)
            if any(
                item.eligibility_decision.registry_allowed
                and item.eligibility_decision.regime_policy.value == "BLOCK"
                for item in evidence
            ):
                metrics.regime_blocked += 1
            if any(
                item.eligibility_decision.registry_allowed
                and item.eligibility_decision.adjusted_confidence
                < item.eligibility_decision.original_confidence
                for item in evidence
            ):
                metrics.confidence_reduced += 1

        conflict_details: list[dict[str, Any]] = []
        for candidate in candidates:
            if candidate.conflict_level == "NONE":
                continue
            context = snapshot_context.get(candidate.feature_snapshot_id)
            conflict_indicator = context[1] if context is not None else None
            all_evidence = (*candidate.supporting_signals, *candidate.opposing_signals)
            buys = [item for item in all_evidence if item.signal.signal_type is SignalType.BUY]
            sells = [item for item in all_evidence if item.signal.signal_type is SignalType.SELL]
            strategies = sorted({item.strategy_name for item in all_evidence})
            conflict_details.append(
                {
                    "instrument": candidate.symbol,
                    "timeframe": candidate.timeframe.value,
                    "settled_candle_timestamp": candidate.settled_candle_timestamp,
                    "buy_strategies": [item.strategy_name for item in buys],
                    "sell_strategies": [item.strategy_name for item in sells],
                    "buy_scores": {item.strategy_name: item.signal.confidence for item in buys},
                    "sell_scores": {item.strategy_name: item.signal.confidence for item in sells},
                    "buy_eligibility": {
                        item.strategy_name: item.eligibility_decision.to_dict() for item in buys
                    },
                    "sell_eligibility": {
                        item.strategy_name: item.eligibility_decision.to_dict() for item in sells
                    },
                    "regime": (candidate.representative_signal.current_regime or "unknown"),
                    "indicators": (
                        {
                            "close": indicator.close,
                            "ema20": indicator.ema.get(20),
                            "ema50": indicator.ema.get(50),
                            "rsi14": conflict_indicator.rsi,
                            "adx14": conflict_indicator.adx,
                            "plus_di": conflict_indicator.plus_di,
                            "minus_di": conflict_indicator.minus_di,
                            "atr14": conflict_indicator.atr,
                            "relative_volume": conflict_indicator.relative_volume,
                            "bb_percent": conflict_indicator.bb_percent,
                            "vwap_distance_atr": conflict_indicator.vwap_distance_atr,
                        }
                        if conflict_indicator is not None
                        else {}
                    ),
                    "strategy_configuration": {
                        name: dict(self.strategy_config.parameters.get(name, {}))
                        for name in strategies
                    },
                    "resolution": candidate.conflict_resolution,
                }
            )
        seen_mtf: set[str] = set()
        for resolution in instrument_resolutions.values():
            if not resolution.conflicted or resolution.symbol in seen_mtf:
                continue
            seen_mtf.add(resolution.symbol)
            conflict_details.append(
                {
                    "instrument": resolution.symbol,
                    "scope": "MULTI_TIMEFRAME",
                    "buy_timeframes": list(resolution.buy_timeframes),
                    "sell_timeframes": list(resolution.sell_timeframes),
                    "resolution": "UNRESOLVED_BLOCK",
                }
            )

        ml_started = perf_counter()
        ml_allowed: dict[str, bool] = {}
        ml_reasons: dict[str, str] = {}
        qwen_allowed: dict[str, bool] = {}
        qwen_review_payloads: list[dict[str, Any]] = []
        qwen_policy = QwenReviewPolicy(
            regime_uncertain_below=float(
                getattr(self.settings, "qwen_regime_uncertain_below", 0.60)
            ),
            ml_conflict_below=float(getattr(self.settings, "qwen_ml_conflict_below", 0.45)),
        )
        for candidate in candidates:
            candidate_key = candidate_identity(candidate)
            lifecycle = lifecycle_by_key[candidate_key]
            ml_allowed[candidate_key] = False
            qwen_allowed[candidate_key] = False
            # An unresolved multi-strategy conflict is never bypassable (it is a
            # data-quality block, not a validation gate); only the PAPER_APPROVED
            # requirement is lifted by allow_unvalidated_paper.
            unresolved_conflict = candidate.conflict_resolution == "UNRESOLVED_BLOCK"
            if not candidate.pipeline_eligible and (
                unresolved_conflict or not self.allow_unvalidated_paper
            ):
                evidence = (*candidate.supporting_signals, *candidate.opposing_signals)
                registry_reasons = [
                    item.eligibility_decision.reason_code
                    for item in evidence
                    if not item.eligibility_decision.registry_allowed
                ]
                regime_blocked = any(
                    item.eligibility_decision.registry_allowed
                    and item.eligibility_decision.regime_policy.value == "BLOCK"
                    for item in evidence
                )
                reason = (
                    "UNRESOLVED_STRATEGY_CONFLICT"
                    if unresolved_conflict
                    else registry_reasons[0]
                    if registry_reasons
                    else "REGIME_BLOCKED"
                    if regime_blocked
                    else candidate.representative_signal.registry_reason
                    or "NOT_PAPER_APPROVED"
                )
                lifecycle.reject(reason, shadow=candidate.shadow_only)
                continue
            lifecycle.advance(
                ForexDecisionStage.ELIGIBILITY_PASSED,
                "STRATEGY_PAPER_APPROVED"
                if candidate.pipeline_eligible
                else "PAPER_APPROVAL_BYPASSED_UNVALIDATED",
            )
            resolution = instrument_resolutions[candidate_key]
            if resolution.conflicted:
                lifecycle.reject("MTF_CONFLICT")
                continue
            if (
                resolution.primary_candidate_id is not None
                and resolution.primary_candidate_id != candidate_key
            ):
                lifecycle.reject("SAME_DIRECTION_CANDIDATE_CONSOLIDATED")
                continue
            lifecycle.advance(ForexDecisionStage.REGIME_PASSED, "REGIME_POLICY_ALLOWED")
            lifecycle.entry_quality = "PASSED_BY_SETTLED_STRATEGY_TRIGGER"
            lifecycle.advance(
                ForexDecisionStage.ENTRY_QUALITY_PASSED,
                lifecycle.entry_quality,
            )
            metrics.entry_quality_passed += 1
            ml_allowed[candidate_key] = True
            qwen_allowed[candidate_key] = True
            representative = candidate.representative_signal
            strategy_name = representative.strategy.value
            policy = MLPolicy(self.strategy_config.ml_policy(strategy_name, candidate.timeframe.value))
            active_model = (
                self.model_registry.latest_active(
                    ModelGrain(
                        market="FOREX",
                        strategy_name=strategy_name,
                        timeframe=candidate.timeframe.value,
                        strategy_version=self.strategy_config.version,
                    )
                )
                if self.model_registry is not None
                else None
            )
            policy_decision = evaluate_ml_policy(
                policy, active_model_available=active_model is not None
            )
            if not policy_decision.run_inference:
                if policy is not MLPolicy.DISABLED:
                    metrics.ml_abstained += 1
                ml_reasons[candidate_key] = policy_decision.reason
                ml_allowed[candidate_key] = not policy_decision.reject_candidate
                lifecycle.ml_status = "SKIPPED" if ml_allowed[candidate_key] else "REJECTED"
                if ml_allowed[candidate_key]:
                    lifecycle.advance(ForexDecisionStage.ML_SKIPPED, policy_decision.reason)
                    lifecycle.qwen_status = "SKIPPED_NO_ML_QUALIFIED_CANDIDATE"
                    lifecycle.advance(
                        ForexDecisionStage.QWEN_SKIPPED,
                        lifecycle.qwen_status,
                    )
                else:
                    lifecycle.reject(policy_decision.reason)
                continue
            assert active_model is not None
            history = self._history_frames.get((candidate.symbol, candidate.timeframe.value))
            if self.prediction_agent is None or history is None:
                metrics.ml_abstained += 1
                ml_reasons[candidate_key] = "FOREX_ML_ARTIFACT_NOT_AVAILABLE"
                ml_allowed[candidate_key] = policy is not MLPolicy.REQUIRED
                lifecycle.ml_status = "ABSTAIN"
                if ml_allowed[candidate_key]:
                    lifecycle.advance(
                        ForexDecisionStage.ML_SKIPPED,
                        "FOREX_ML_ARTIFACT_NOT_AVAILABLE",
                    )
                    lifecycle.qwen_status = "SKIPPED_NO_ML_QUALIFIED_CANDIDATE"
                    lifecycle.advance(
                        ForexDecisionStage.QWEN_SKIPPED,
                        lifecycle.qwen_status,
                    )
                else:
                    lifecycle.reject("FOREX_ML_ARTIFACT_NOT_AVAILABLE")
                continue
            prediction = await asyncio.to_thread(
                self.prediction_agent.predict_cached,
                history,
                candidate.symbol,
                timeframe=candidate.timeframe.value,
                trade_horizon=candidate.trade_horizon.value,
                strategy_version=self.strategy_config.version,
                regime=representative.current_regime,
                market="FOREX",
                instrument_scope="*",
                model_version=active_model.model_version,
            )
            metrics.ml_evaluated += 1
            representative.ml_model_version = prediction.model_version
            representative.ml_feature_version = prediction.feature_version
            if prediction.abstained:
                metrics.ml_abstained += 1
                representative.ml_status = "ABSTAIN"
                ml_reasons[candidate_key] = prediction.reasoning
                ml_allowed[candidate_key] = policy is not MLPolicy.REQUIRED
                lifecycle.ml_status = "ABSTAIN"
                if ml_allowed[candidate_key]:
                    lifecycle.advance(ForexDecisionStage.ML_SKIPPED, prediction.reasoning)
                else:
                    lifecycle.reject(prediction.reasoning)
            else:
                representative.ml_status = "VALIDATED"
                probability_up = (
                    prediction.confidence
                    if prediction.direction == "up"
                    else 1.0 - prediction.confidence
                )
                representative.ml_probability = probability_up
                expected_direction = "up" if candidate.direction is SignalType.BUY else "down"
                threshold = (
                    candidate.strongest_validated_signal.eligibility.minimum_model_confidence
                    if candidate.strongest_validated_signal is not None
                    and candidate.strongest_validated_signal.eligibility is not None
                    else 0.0
                )
                qualified = (
                    prediction.direction == expected_direction
                    and prediction.confidence >= threshold
                )
                ml_allowed[candidate_key] = qualified
                if qualified:
                    metrics.ml_qualified += 1
                    lifecycle.ml_status = "PASSED"
                    lifecycle.advance(
                        ForexDecisionStage.ML_PASSED,
                        f"MODEL_{prediction.model_version}",
                    )
                else:
                    ml_reasons[candidate_key] = "FOREX_ML_DIRECTION_OR_CONFIDENCE_REJECT"
                    lifecycle.ml_status = "REJECTED"
                    lifecycle.reject("FOREX_ML_DIRECTION_OR_CONFIDENCE_REJECT")
            if self.trading_repository is not None:
                prediction_id = forex_record_id(
                    "ml", candidate_key, prediction.model_version, prediction.feature_version
                )
                await asyncio.to_thread(
                    self.trading_repository.persist_ml_prediction,
                    prediction_id=prediction_id,
                    symbol=candidate.symbol,
                    timeframe=candidate.timeframe.value,
                    settled_candle_timestamp=candidate.settled_candle_timestamp,
                    model_version=prediction.model_version,
                    payload=prediction.to_dict(),
                )
                metrics.persistence_writes += 1

            if not ml_allowed[candidate_key] or prediction.abstained:
                if ml_allowed[candidate_key]:
                    lifecycle.qwen_status = "SKIPPED_NO_ML_QUALIFIED_CANDIDATE"
                    lifecycle.advance(
                        ForexDecisionStage.QWEN_SKIPPED,
                        lifecycle.qwen_status,
                    )
                continue
            payload = candidate.to_dict()
            payload["signal_id"] = lifecycle.qwen_review_id
            payload["candidate_id"] = candidate_key
            payload["candidate_version"] = lifecycle.candidate_version
            payload["regime"] = representative.current_regime or "unknown"
            review_decision = qwen_policy.assess(payload, regime_confidence=1.0, shared_context={})
            if review_decision.required:
                metrics.qwen_required += 1
                qwen_allowed[candidate_key] = False
                qwen_review_payloads.append(payload)
                lifecycle.qwen_status = "REQUIRED"
            else:
                metrics.qwen_skipped_clear += 1
                lifecycle.qwen_status = "SKIPPED_CLEAR"
                lifecycle.advance(
                    ForexDecisionStage.QWEN_SKIPPED,
                    "DETERMINISTIC_CONTEXT_CLEAR",
                )
        metrics.ml_duration_ms = (perf_counter() - ml_started) * 1000

        qwen_started = perf_counter()
        if qwen_review_payloads:
            active_strategies = sorted(
                {
                    str(strategy)
                    for payload in qwen_review_payloads
                    for strategy in payload.get("supporting_strategies", [])
                }
            )
            review_result = await asyncio.to_thread(
                signal_validation_node,
                cast(TradingState, {
                    "workflow_id": f"forex:{trade_horizon.value}:{datetime.now().isoformat()}",
                    "signals": qwen_review_payloads,
                    "trade_horizon": trade_horizon.value,
                    "regime": "MULTI",
                    "regime_confidence": 1.0,
                    "active_strategies": active_strategies,
                    "shared_market_context": {"context_version": "forex-settled-candle-v1"},
                    "memory_lessons": [],
                    "llm_optimization_metrics": {},
                    "errors": [],
                }),
            )
            validated_ids = {
                str(item.get("signal_id", ""))
                for item in review_result.get("validated_signals", [])
            }
            review_metrics = dict(review_result.get("llm_optimization_metrics", {}))
            metrics.qwen_external_calls += int(review_metrics.get("qwen_calls", 0))
            metrics.qwen_cache_hits += int(review_metrics.get("qwen_cache_hits", 0))
            metrics.qwen_deferred += int(review_metrics.get("qwen_reviews_deferred", 0))
            metrics.qwen_input_tokens += int(review_metrics.get("qwen_input_tokens", 0))
            metrics.qwen_output_tokens += int(review_metrics.get("qwen_output_tokens", 0))
            metrics.qwen_reviewed += int(review_metrics.get("qwen_approved", 0)) + len(
                review_result.get("rejected_signals", [])
            )
            for payload in qwen_review_payloads:
                candidate_key = str(payload["candidate_id"])
                review_id = str(payload["signal_id"])
                approved = review_id in validated_ids
                qwen_allowed[candidate_key] = approved
                lifecycle = lifecycle_by_key[candidate_key]
                lifecycle.qwen_status = "PASSED" if approved else "REJECTED"
                if approved:
                    lifecycle.advance(
                        ForexDecisionStage.QWEN_PASSED,
                        "CONTEXT_REVIEW_APPROVED",
                    )
                else:
                    lifecycle.reject("QWEN_CONTEXT_REJECT")
        metrics.qwen_duration_ms = (perf_counter() - qwen_started) * 1000

        prices = (
            await self.provider.get_prices(sorted(scanned_instruments))
            if scanned_instruments
            else {}
        )
        instruments_by_symbol = {item.symbol: item for item in instruments}
        conversion_rates = {
            (
                instruments_by_symbol[symbol].base_currency,
                instruments_by_symbol[symbol].quote_currency,
            ): quote.mid
            for symbol, quote in prices.items()
            if symbol in instruments_by_symbol and quote.mid > 0
        }
        account_equity = float(getattr(self.settings, "forex_paper_account_equity", 100000.0))
        home_currency = str(getattr(self.settings, "forex_account_home_currency", "USD"))
        open_position_payloads: list[dict[str, Any]] = []
        if self.trading_repository is not None:
            try:
                open_position_payloads = await asyncio.to_thread(
                    self.trading_repository.load_open_positions
                )
            except Exception as exc:
                metrics.errors.append(f"FOREX_POSITION_STATE_UNAVAILABLE:{type(exc).__name__}")
        portfolio_notional_home = sum(
            float(item.get("notional_home", 0.0) or 0.0) for item in open_position_payloads
        )
        margin_used_home = sum(
            float(item.get("margin_required_home", 0.0) or 0.0) for item in open_position_payloads
        )
        currency_exposure_home: dict[str, float] = {}
        for position_payload in open_position_payloads:
            for currency, amount in dict(
                position_payload.get("currency_exposure_home", {})
            ).items():
                normalized = str(currency).upper()
                currency_exposure_home[normalized] = currency_exposure_home.get(
                    normalized, 0.0
                ) + float(amount)
        risk_started = perf_counter()
        for candidate in candidates:
            candidate_key = candidate_identity(candidate)
            lifecycle = lifecycle_by_key[candidate_key]
            if lifecycle.rejection_reason is not None:
                if lifecycle.final_action.startswith("SHADOW_"):
                    metrics.shadow_decisions += 1
                if self.trading_repository is not None:
                    persistence_started = perf_counter()
                    await asyncio.to_thread(
                        persist_lifecycle_record,
                        self.trading_repository,
                        lifecycle,
                    )
                    metrics.persistence_writes += 1
                    metrics.persistence_duration_ms += (
                        perf_counter() - persistence_started
                    ) * 1000
                continue
            if not (
                (candidate.execution_allowed or self.allow_unvalidated_paper)
                and ml_allowed.get(candidate_key, False)
                and qwen_allowed.get(candidate_key, False)
            ):
                lifecycle.reject(
                    ml_reasons.get(candidate_key, "PRE_RISK_QUALIFICATION_BLOCKED")
                )
                if self.trading_repository is not None:
                    await asyncio.to_thread(
                        persist_lifecycle_record,
                        self.trading_repository,
                        lifecycle,
                    )
                    metrics.persistence_writes += 1
                continue
            if any(
                str(position.get("candidate_id", "")) == candidate_key
                for position in open_position_payloads
            ):
                # A restart may rediscover the same settled-candle decision.  Keep
                # the original persisted final lifecycle authoritative instead of
                # overwriting it with a duplicate-position rejection.
                lifecycle.reject("DUPLICATE_POSITION")
                continue
            quote = prices.get(candidate.symbol)
            context = snapshot_context.get(candidate.feature_snapshot_id)
            if quote is None or context is None:
                lifecycle.reject("STALE_PRICE" if quote is None else "FEATURE_CONTEXT_MISSING")
                if self.trading_repository is not None:
                    await asyncio.to_thread(
                        persist_lifecycle_record,
                        self.trading_repository,
                        lifecycle,
                    )
                    metrics.persistence_writes += 1
                continue
            instrument, indicator = context
            side = PriceSide(candidate.direction.value)
            stop_price = float(candidate.representative_signal.stop_loss)
            target_price = float(candidate.representative_signal.target_price)
            size = None
            sizing_error: str | None = None
            try:
                size = size_forex_position(
                    instrument=instrument,
                    quote=quote,
                    side=side,
                    stop_price=stop_price,
                    account_equity=account_equity,
                    risk_fraction=self.risk_limits.risk_per_trade,
                    account_home_currency=home_currency,
                    conversion_rates=conversion_rates,
                )
            except ValueError as exc:
                sizing_error = str(exc)
            quote_home_rate = 0.0
            if size is not None:
                try:
                    quote_home_rate = quote_to_home_rate(
                        instrument, home_currency, conversion_rates
                    )
                except ValueError:
                    sizing_error = "MISSING_ACCOUNT_CURRENCY_CONVERSION"
            if sizing_error is not None or size is None or size.units <= 0:
                lifecycle.reject(
                    sizing_error
                    or (size.reason if size is not None else "FOREX_SIZING_FAILED")
                )
                if self.trading_repository is not None:
                    await asyncio.to_thread(
                        persist_lifecycle_record,
                        self.trading_repository,
                        lifecycle,
                    )
                    metrics.persistence_writes += 1
                continue
            notional_home = (
                size.units * size.entry_price * quote_home_rate
            )
            required_margin_home = notional_home * float(instrument.margin_rate or 1.0)
            same_currency_positions = sum(
                1
                for item in open_position_payloads
                if {
                    str(item.get("base_currency", "")).upper(),
                    str(item.get("quote_currency", "")).upper(),
                }.intersection({instrument.base_currency, instrument.quote_currency})
            )
            exposure_ratios = {
                currency: amount / account_equity
                for currency, amount in currency_exposure_home.items()
            }
            risk = evaluate_forex_pretrade(
                instrument=instrument,
                quote=quote,
                side=side,
                atr=float(indicator.atr or 0.0),
                open_positions=len(open_position_payloads),
                portfolio_exposure_ratio=portfolio_notional_home / account_equity,
                currency_exposure_ratios=exposure_ratios,
                correlated_currency_positions=same_currency_positions,
                limits=self.risk_limits,
                global_kill_switch=bool(getattr(self.settings, "global_kill_switch", False)),
                forex_kill_switch=bool(getattr(self.settings, "forex_kill_switch", False)),
                stop_price=stop_price,
                risk_amount_home=size.risk_amount_home,
                account_equity_home=account_equity,
                margin_used_home=margin_used_home,
                required_margin_home=required_margin_home,
                duplicate_position=any(
                    str(position.get("symbol", "")).upper() == candidate.symbol
                    for position in open_position_payloads
                ),
                require_stop_loss=True,
                now=datetime.now(quote.timestamp.tzinfo),
            )
            if not risk.approved:
                metrics.risk_blocked += 1
                lifecycle.risk_result = "BLOCKED"
                lifecycle.reject(
                    risk.reason_codes[0] if risk.reason_codes else "RISK_BLOCKED"
                )
                if self.trading_repository is not None:
                    persistence_started = perf_counter()
                    await asyncio.to_thread(
                        persist_lifecycle_record,
                        self.trading_repository,
                        lifecycle,
                    )
                    metrics.persistence_writes += 1
                    metrics.persistence_duration_ms += (
                        perf_counter() - persistence_started
                    ) * 1000
                continue

            metrics.risk_approved += 1
            lifecycle.risk_result = "APPROVED"
            lifecycle.advance(ForexDecisionStage.RISK_APPROVED, "DETERMINISTIC_RISK_PASS")
            if self.trading_repository is None:
                lifecycle.reject("PAPER_PERSISTENCE_UNAVAILABLE")
                metrics.risk_blocked += 1
                continue

            signed = 1.0 if candidate.direction is SignalType.BUY else -1.0
            currency_delta = {
                instrument.base_currency: signed * notional_home,
                instrument.quote_currency: -signed * notional_home,
            }
            paper_payload = {
                "market": "FOREX",
                "provider": "OANDA",
                "execution": "PAPER",
                "broker_orders": "OFF",
                "status": "OPEN",
                "symbol": candidate.symbol,
                "side": candidate.direction.value,
                "units": size.units,
                "entry_price": size.entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "risk_amount_home": size.risk_amount_home,
                "notional_home": notional_home,
                "margin_required_home": required_margin_home,
                "account_home_currency": home_currency,
                "base_currency": instrument.base_currency,
                "quote_currency": instrument.quote_currency,
                "currency_exposure_home": currency_delta,
                "settled_candle_timestamp": candidate.settled_candle_timestamp,
            }
            persistence_started = perf_counter()
            paper_fill_created = await asyncio.to_thread(
                create_local_paper_position,
                self.trading_repository,
                lifecycle,
                quantity=size.units,
                position_payload=paper_payload,
            )
            metrics.persistence_writes += 3 if paper_fill_created else 2
            metrics.persistence_duration_ms += (
                perf_counter() - persistence_started
            ) * 1000
            if paper_fill_created:
                open_position_payloads.append(
                    {
                        **paper_payload,
                        "decision_id": lifecycle.decision_id,
                        "candidate_id": lifecycle.candidate_id,
                        "position_id": lifecycle.position_id,
                    }
                )
                portfolio_notional_home += notional_home
                margin_used_home += required_margin_home
                for currency, amount in currency_delta.items():
                    currency_exposure_home[currency] = (
                        currency_exposure_home.get(currency, 0.0) + amount
                    )
                metrics.paper_orders += 1
                if candidate.direction is SignalType.BUY:
                    metrics.final_buy += 1
                else:
                    metrics.final_sell += 1
            else:
                metrics.risk_blocked += 1
        metrics.risk_duration_ms = (perf_counter() - risk_started) * 1000
        decision_records = []
        for lifecycle in lifecycle_by_key.values():
            payload = lifecycle.to_dict()
            if lifecycle.final_action.startswith("PAPER_"):
                status = DecisionStatus.PAPER_FILLED
                reasons: tuple[str, ...] = ()
            elif lifecycle.final_action == "REJECT" or lifecycle.final_action.startswith("SHADOW_"):
                status = DecisionStatus.REJECTED
                reasons = (lifecycle.rejection_reason or "DECISION_REJECTED",)
            elif lifecycle.risk_result == "APPROVED":
                status = DecisionStatus.RISK_APPROVED
                reasons = ()
            else:
                status = DecisionStatus.CANDIDATE
                reasons = ()
            decision_records.append(
                decision_record_from_payload(
                    market="FOREX",
                    provider="OANDA",
                    payload=payload,
                    status=status,
                    rejection_reasons=reasons,
                )
            )
        metrics.persistence_writes += await asyncio.to_thread(
            persist_decision_records,
            self.decision_repository,
            decision_records,
        )
        metrics.total_duration_ms = (perf_counter() - started) * 1000
        return ForexCycleResult(
            metrics,
            tuple(snapshots),
            tuple(candidates),
            tuple(conflict_details),
            tuple(record.to_dict() for record in lifecycle_by_key.values()),
        )
