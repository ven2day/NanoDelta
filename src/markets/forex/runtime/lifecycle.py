"""Auditable lifecycle models for local-paper Forex decisions.

Technical candidates are diagnostic evidence.  Only records that progress through
eligibility, context/ML policy, deterministic risk, and a persisted local-paper fill
are final decisions or positions.  This module deliberately has no broker client.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from src.core.aggregation.consolidation import AggregatedCandidate


class ForexDecisionStage(StrEnum):
    TECHNICAL_SIGNAL = "TECHNICAL_SIGNAL"
    CONSOLIDATED_CANDIDATE = "CONSOLIDATED_CANDIDATE"
    ELIGIBILITY_PASSED = "ELIGIBILITY_PASSED"
    REGIME_PASSED = "REGIME_PASSED"
    ENTRY_QUALITY_PASSED = "ENTRY_QUALITY_PASSED"
    ML_PASSED = "ML_PASSED"
    ML_SKIPPED = "ML_SKIPPED"
    QWEN_PASSED = "QWEN_PASSED"
    QWEN_SKIPPED = "QWEN_SKIPPED"
    RISK_APPROVED = "RISK_APPROVED"
    FINAL_PAPER_DECISION = "FINAL_PAPER_DECISION"
    PAPER_ORDER_CREATED = "PAPER_ORDER_CREATED"
    POSITION_OPEN = "POSITION_OPEN"
    POSITION_CLOSED = "POSITION_CLOSED"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _stable_hash(kind: str, *parts: object) -> str:
    canonical = "|".join([kind, *(str(part).strip() for part in parts)])
    return f"forex-{kind}-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]}"


def candidate_version(candidate: AggregatedCandidate) -> str:
    """Version the material technical evidence without quote-only churn."""

    payload = candidate.to_dict()
    material = {
        "feature_version": payload.get("feature_version", ""),
        "strategy_consensus_identity": payload.get("strategy_consensus_identity", ""),
        "technical_quality": payload.get("technical_quality"),
        "conflict_level": payload.get("conflict_level", ""),
        "conflict_resolution": payload.get("conflict_resolution", ""),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def candidate_identity(candidate: AggregatedCandidate) -> str:
    return _stable_hash(
        "candidate",
        "FOREX",
        candidate.symbol.upper(),
        candidate.timeframe.value,
        candidate.direction.value,
        candidate.settled_candle_timestamp,
        candidate_version(candidate),
    )


def qwen_review_identity(candidate: AggregatedCandidate) -> str:
    """Stable paid-review identity; a scheduler wake or small quote move is irrelevant."""

    return _stable_hash(
        "qwen-review",
        "FOREX",
        candidate.symbol.upper(),
        candidate.timeframe.value,
        candidate.direction.value,
        candidate.settled_candle_timestamp,
        candidate_version(candidate),
    )


@dataclass(frozen=True)
class InstrumentConflict:
    symbol: str
    conflicted: bool
    buy_timeframes: tuple[str, ...] = ()
    sell_timeframes: tuple[str, ...] = ()
    primary_candidate_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "conflicted": self.conflicted,
            "buy_timeframes": list(self.buy_timeframes),
            "sell_timeframes": list(self.sell_timeframes),
            "primary_candidate_id": self.primary_candidate_id,
        }


def _candidate_evidence(candidate: AggregatedCandidate) -> tuple[float, float, str, str]:
    validated = candidate.strongest_validated_signal
    profit_factor = (
        float(validated.eligibility.oos_profit_factor or 0.0)
        if validated is not None and validated.eligibility is not None
        else 0.0
    )
    technical_quality = float(candidate.technical_quality or 0.0)
    return (
        profit_factor,
        technical_quality,
        candidate.settled_candle_timestamp,
        candidate.timeframe.value,
    )


def classify_instrument_conflicts(
    candidates: Iterable[AggregatedCandidate],
) -> dict[str, InstrumentConflict]:
    """Classify opposing timeframes and select one same-direction executable setup.

    Every timeframe remains visible as technical evidence.  Opposing directions are
    never resolved by Qwen.  For aligned executable candidates, validated OOS profit
    factor and then technical quality select one order path; the other timeframes are
    retained as supporting evidence and cannot create duplicate positions.
    """

    by_symbol: dict[str, list[AggregatedCandidate]] = {}
    for candidate in candidates:
        by_symbol.setdefault(candidate.symbol.upper(), []).append(candidate)

    result: dict[str, InstrumentConflict] = {}
    for symbol, group in by_symbol.items():
        buys = tuple(sorted({item.timeframe.value for item in group if item.direction.value == "BUY"}))
        sells = tuple(
            sorted({item.timeframe.value for item in group if item.direction.value == "SELL"})
        )
        conflicted = bool(buys and sells)
        executable = [item for item in group if item.execution_allowed]
        primary = None
        if not conflicted and executable:
            primary = candidate_identity(max(executable, key=_candidate_evidence))
        resolution = InstrumentConflict(
            symbol=symbol,
            conflicted=conflicted,
            buy_timeframes=buys,
            sell_timeframes=sells,
            primary_candidate_id=primary,
        )
        for candidate in group:
            result[candidate_identity(candidate)] = resolution
    return result


def update_candidate_state(
    state: dict[tuple[str, str], AggregatedCandidate],
    *,
    changed_grains: Iterable[tuple[str, str]],
    current_candidates: Iterable[AggregatedCandidate],
) -> tuple[AggregatedCandidate, ...]:
    """Retain the latest settled technical candidate independently per timeframe.

    A changed timeframe first invalidates its prior candidate.  If the new settled
    candle emits a candidate it is then installed as the latest state.  Unchanged
    higher-timeframe evidence remains available for deterministic conflict checks.
    """

    for symbol, timeframe in changed_grains:
        state.pop((symbol.strip().upper(), timeframe.strip().lower()), None)
    for candidate in current_candidates:
        state[(candidate.symbol.upper(), candidate.timeframe.value.lower())] = candidate
    return tuple(state.values())


@dataclass
class ForexDecisionLifecycle:
    decision_id: str
    candidate_id: str
    feature_snapshot_id: str
    candidate_version: str
    qwen_review_id: str
    instrument: str
    timeframe: str
    side: str
    strategy: str
    supporting_strategies: list[str]
    settled_candle_timestamp: str
    created_at: str
    entry_price: float
    stop_price: float
    target_price: float
    technical_score: float | None
    eligibility_status: str
    current_regime: str
    regime_policy: str
    current_stage: ForexDecisionStage = ForexDecisionStage.CONSOLIDATED_CANDIDATE
    rejection_reason: str | None = None
    final_action: str = "NONE"
    risk_result: str = "NOT_EVALUATED"
    ml_status: str = "NOT_EVALUATED"
    qwen_status: str = "NOT_EVALUATED"
    entry_quality: str = "NOT_EVALUATED"
    order_id: str | None = None
    position_id: str | None = None
    multi_timeframe: dict[str, Any] = field(default_factory=dict)
    stage_history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_candidate(cls, candidate: AggregatedCandidate) -> ForexDecisionLifecycle:
        payload = candidate.to_dict()
        candidate_id = candidate_identity(candidate)
        version = candidate_version(candidate)
        record = cls(
            decision_id=_stable_hash("decision", candidate_id),
            candidate_id=candidate_id,
            feature_snapshot_id=candidate.feature_snapshot_id,
            candidate_version=version,
            qwen_review_id=qwen_review_identity(candidate),
            instrument=candidate.symbol.upper(),
            timeframe=candidate.timeframe.value,
            side=candidate.direction.value,
            strategy=str(payload.get("strategy", "")),
            supporting_strategies=[str(item) for item in payload.get("supporting_strategies", [])],
            settled_candle_timestamp=candidate.settled_candle_timestamp,
            created_at=_utc_now(),
            entry_price=float(payload.get("entry_price", 0.0) or 0.0),
            stop_price=float(payload.get("stop_loss", 0.0) or 0.0),
            target_price=float(payload.get("target_price", 0.0) or 0.0),
            technical_score=(
                float(candidate.technical_quality)
                if candidate.technical_quality is not None
                else None
            ),
            eligibility_status=str(payload.get("eligibility_status", "UNVALIDATED")),
            current_regime=str(payload.get("current_regime", "unknown")),
            regime_policy=str(payload.get("regime_policy", "ALLOW")),
        )
        record._record_stage(ForexDecisionStage.TECHNICAL_SIGNAL, "STRATEGY_SIGNAL_GENERATED")
        record._record_stage(
            ForexDecisionStage.CONSOLIDATED_CANDIDATE,
            "STRATEGY_SIGNALS_CONSOLIDATED",
        )
        return record

    def _record_stage(self, stage: ForexDecisionStage, reason: str | None = None) -> None:
        self.current_stage = stage
        self.stage_history.append(
            {"stage": stage.value, "at": _utc_now(), "reason": reason}
        )

    def advance(self, stage: ForexDecisionStage, reason: str | None = None) -> None:
        self.rejection_reason = None
        self._record_stage(stage, reason)

    def reject(self, reason: str, *, shadow: bool = False) -> None:
        self.rejection_reason = reason
        self.final_action = f"SHADOW_{self.side}" if shadow else "REJECT"

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "candidate_id": self.candidate_id,
            "feature_snapshot_id": self.feature_snapshot_id,
            "candidate_version": self.candidate_version,
            "qwen_review_id": self.qwen_review_id,
            "market": "FOREX",
            "provider": "OANDA",
            "execution": "PAPER",
            "broker_orders": "OFF",
            "instrument": self.instrument,
            "symbol": self.instrument,
            "timeframe": self.timeframe,
            "side": self.side,
            "signal_type": self.side,
            "strategy": self.strategy,
            "supporting_strategies": list(self.supporting_strategies),
            "settled_candle_timestamp": self.settled_candle_timestamp,
            "created_at": self.created_at,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "stop_loss": self.stop_price,
            "target_price": self.target_price,
            "technical_score": self.technical_score,
            "eligibility_status": self.eligibility_status,
            "current_regime": self.current_regime,
            "regime_policy": self.regime_policy,
            "current_stage": self.current_stage.value,
            "decision_status": self.current_stage.value,
            "rejection_reason": self.rejection_reason,
            "final_action": self.final_action,
            "risk_result": self.risk_result,
            "entry_quality": self.entry_quality,
            "ml_status": self.ml_status,
            "qwen_status": self.qwen_status,
            "order_id": self.order_id,
            "position_id": self.position_id,
            "multi_timeframe": dict(self.multi_timeframe),
            "stage_history": list(self.stage_history),
        }


FINAL_DECISION_STAGES = frozenset(
    {
        ForexDecisionStage.FINAL_PAPER_DECISION.value,
        ForexDecisionStage.PAPER_ORDER_CREATED.value,
        ForexDecisionStage.POSITION_OPEN.value,
        ForexDecisionStage.POSITION_CLOSED.value,
    }
)


def append_persisted_stage(
    payload: dict[str, Any], stage: ForexDecisionStage, reason: str | None = None
) -> dict[str, Any]:
    updated = dict(payload)
    history = [dict(item) for item in updated.get("stage_history", []) if isinstance(item, dict)]
    history.append({"stage": stage.value, "at": _utc_now(), "reason": reason})
    updated["current_stage"] = stage.value
    updated["decision_status"] = stage.value
    updated["stage_history"] = history
    return updated


__all__ = [
    "FINAL_DECISION_STAGES",
    "ForexDecisionLifecycle",
    "ForexDecisionStage",
    "InstrumentConflict",
    "append_persisted_stage",
    "candidate_identity",
    "candidate_version",
    "classify_instrument_conflicts",
    "qwen_review_identity",
    "update_candidate_state",
]
