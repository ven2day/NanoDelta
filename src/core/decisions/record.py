"""Versioned Decision-layer records shared by every market runtime."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

import pandas as pd

from src.core.models import Market, PriceSide
from src.core.pipeline import ProcessingRole


class DecisionStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    REJECTED = "REJECTED"
    RISK_APPROVED = "RISK_APPROVED"
    PAPER_APPROVED = "PAPER_APPROVED"
    PAPER_FILLED = "PAPER_FILLED"


class EvidenceVerdict(StrEnum):
    SUPPORT = "SUPPORT"
    CAUTION = "CAUTION"
    REJECT = "REJECT"
    ABSTAIN = "ABSTAIN"
    APPROVE = "APPROVE"


@dataclass(frozen=True)
class DecisionEvidence:
    producer: ProcessingRole
    evidence_type: str
    verdict: EvidenceVerdict
    payload: Mapping[str, Any]

    @classmethod
    def create(
        cls,
        *,
        producer: ProcessingRole | str,
        evidence_type: str,
        verdict: EvidenceVerdict | str,
        payload: Mapping[str, Any] | None = None,
    ) -> DecisionEvidence:
        role = ProcessingRole(str(producer).strip().upper())
        if role not in {
            ProcessingRole.STRATEGY_ENGINE,
            ProcessingRole.ML_INFERENCE,
            ProcessingRole.TRADING_AGENTS,
            ProcessingRole.RISK_ENGINE,
        }:
            raise ValueError(f"{role.value} cannot produce Decision evidence")
        normalized_verdict = EvidenceVerdict(str(verdict).strip().upper())
        if normalized_verdict is EvidenceVerdict.APPROVE and role is not ProcessingRole.RISK_ENGINE:
            raise ValueError("Only deterministic risk may approve a Decision")
        return cls(
            producer=role,
            evidence_type=evidence_type.strip().upper(),
            verdict=normalized_verdict,
            payload=MappingProxyType(dict(payload or {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "producer": self.producer.value,
            "evidence_type": self.evidence_type,
            "verdict": self.verdict.value,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    decision_version: str
    market: Market
    provider: str
    candidate_id: str
    feature_snapshot_id: str | None
    symbol: str
    timeframe: str
    side: PriceSide
    settled_candle_timestamp: datetime
    status: DecisionStatus
    final_action: str
    rejection_reasons: tuple[str, ...]
    evidence: tuple[DecisionEvidence, ...]
    payload: Mapping[str, Any]
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        market: Market | str,
        provider: str,
        candidate_id: str,
        symbol: str,
        timeframe: str,
        side: PriceSide | str,
        settled_candle_timestamp: datetime | str,
        status: DecisionStatus | str,
        evidence: Sequence[DecisionEvidence],
        payload: Mapping[str, Any],
        feature_snapshot_id: str | None = None,
        rejection_reasons: Sequence[str] = (),
        final_action: str | None = None,
        decision_version: str = "decision-v1",
        updated_at: datetime | None = None,
    ) -> DecisionRecord:
        normalized_market = Market.parse(market)
        normalized_side = PriceSide(str(side).strip().upper())
        normalized_status = DecisionStatus(str(status).strip().upper())
        candidate_id = candidate_id.strip()
        if not candidate_id:
            raise ValueError("Decision candidate_id cannot be empty")
        timestamp = pd.Timestamp(settled_candle_timestamp)
        if timestamp.tzinfo is None:
            raise ValueError("Decision settled timestamp must be timezone-aware")
        reasons = tuple(str(item).strip() for item in rejection_reasons if str(item).strip())
        if normalized_status is DecisionStatus.REJECTED and not reasons:
            raise ValueError("Rejected Decision records require a rejection reason")
        if normalized_status is not DecisionStatus.REJECTED and reasons:
            raise ValueError("Only rejected Decision records may carry rejection reasons")
        if normalized_status in {
            DecisionStatus.RISK_APPROVED,
            DecisionStatus.PAPER_APPROVED,
            DecisionStatus.PAPER_FILLED,
        } and not any(
            item.producer is ProcessingRole.RISK_ENGINE
            and item.verdict is EvidenceVerdict.APPROVE
            for item in evidence
        ):
            raise ValueError("Approved Decision records require deterministic risk approval")
        action = final_action or (
            "REJECT"
            if normalized_status is DecisionStatus.REJECTED
            else f"PAPER_{normalized_side.value}"
            if normalized_status in {DecisionStatus.PAPER_APPROVED, DecisionStatus.PAPER_FILLED}
            else "NONE"
        )
        identity = f"{normalized_market.value}|{candidate_id}|{decision_version}"
        decision_id = hashlib.sha256(identity.encode()).hexdigest()
        return cls(
            decision_id=decision_id,
            decision_version=decision_version,
            market=normalized_market,
            provider=provider.strip().upper(),
            candidate_id=candidate_id,
            feature_snapshot_id=(feature_snapshot_id.strip() if feature_snapshot_id else None),
            symbol=symbol.strip().upper(),
            timeframe=timeframe.strip().lower(),
            side=normalized_side,
            settled_candle_timestamp=timestamp.tz_convert("UTC").to_pydatetime(),
            status=normalized_status,
            final_action=action.strip().upper(),
            rejection_reasons=reasons,
            evidence=tuple(evidence),
            payload=MappingProxyType(dict(payload)),
            updated_at=(updated_at or datetime.now(UTC)).astimezone(UTC),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "decision_version": self.decision_version,
            "market": self.market.value,
            "provider": self.provider,
            "candidate_id": self.candidate_id,
            "feature_snapshot_id": self.feature_snapshot_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": self.side.value,
            "settled_candle_timestamp": self.settled_candle_timestamp.isoformat(),
            "status": self.status.value,
            "final_action": self.final_action,
            "rejection_reasons": list(self.rejection_reasons),
            "evidence": [item.to_dict() for item in self.evidence],
            "payload": dict(self.payload),
            "updated_at": self.updated_at.isoformat(),
        }


def stable_candidate_id(market: Market | str, payload: Mapping[str, Any]) -> str:
    """Resolve an existing runtime identity or derive one from settled evidence."""

    for key in ("candidate_id", "signal_id", "candidate_fingerprint"):
        value = str(payload.get(key, "")).strip()
        if value:
            return value
    material = {
        "market": Market.parse(market).value,
        "symbol": str(payload.get("symbol", payload.get("instrument", ""))).upper(),
        "timeframe": str(payload.get("timeframe", "")).lower(),
        "side": str(payload.get("signal_type", payload.get("side", ""))).upper(),
        "settled": str(payload.get("settled_candle_timestamp", "")),
        "feature_snapshot_id": str(payload.get("feature_snapshot_id", "")),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def decision_record_from_payload(
    *,
    market: Market | str,
    provider: str,
    payload: Mapping[str, Any],
    status: DecisionStatus | str,
    rejection_reasons: Sequence[str] = (),
) -> DecisionRecord:
    """Adapt existing NSE/Forex runtime payloads without changing their authorities."""

    normalized_status = DecisionStatus(str(status).strip().upper())
    evidence = [
        DecisionEvidence.create(
            producer=ProcessingRole.STRATEGY_ENGINE,
            evidence_type="TECHNICAL_CANDIDATE",
            verdict=(
                EvidenceVerdict.SUPPORT
                if bool(payload.get("pipeline_eligible", True))
                else EvidenceVerdict.CAUTION
            ),
            payload={
                "strategy": payload.get("strategy"),
                "supporting_strategies": payload.get("supporting_strategies", []),
                "technical_quality": payload.get(
                    "technical_quality", payload.get("technical_score")
                ),
                "eligibility_status": payload.get("eligibility_status"),
            },
        )
    ]
    ml_status = str(payload.get("ml_status", "")).upper()
    if ml_status:
        evidence.append(
            DecisionEvidence.create(
                producer=ProcessingRole.ML_INFERENCE,
                evidence_type="ML_INFERENCE",
                verdict=(
                    EvidenceVerdict.ABSTAIN
                    if "ABSTAIN" in ml_status or "NOT_EVALUATED" in ml_status
                    else EvidenceVerdict.REJECT
                    if "REJECT" in ml_status or "BLOCK" in ml_status
                    else EvidenceVerdict.SUPPORT
                ),
                payload={"status": ml_status, "confidence": payload.get("ml_confidence")},
            )
        )
    validation = payload.get("validation")
    qwen_status = str(payload.get("qwen_status", "")).upper()
    if isinstance(validation, Mapping) or qwen_status:
        validation_payload = dict(validation) if isinstance(validation, Mapping) else {}
        handling = str(
            validation_payload.get("handling", validation_payload.get("decision", qwen_status))
        ).upper()
        evidence.append(
            DecisionEvidence.create(
                producer=ProcessingRole.TRADING_AGENTS,
                evidence_type="CONTEXT_REVIEW",
                verdict=(
                    EvidenceVerdict.REJECT
                    if handling in {"REJECT", "CONTEXT_REJECT", "MATERIAL_CONFLICT"}
                    else EvidenceVerdict.ABSTAIN
                    if "SKIP" in handling or "NOT_EVALUATED" in handling
                    else EvidenceVerdict.SUPPORT
                ),
                payload={**validation_payload, "status": qwen_status},
            )
        )
    risk_payload = payload.get("risk_result")
    risk_status = str(payload.get("risk_result", payload.get("risk_decision", ""))).upper()
    if isinstance(risk_payload, Mapping):
        risk_status = str(risk_payload.get("decision", risk_payload.get("status", risk_status))).upper()
        normalized_risk_payload = dict(risk_payload)
    else:
        normalized_risk_payload = {"status": risk_status}
    if risk_status or normalized_status in {
        DecisionStatus.REJECTED,
        DecisionStatus.RISK_APPROVED,
        DecisionStatus.PAPER_APPROVED,
        DecisionStatus.PAPER_FILLED,
    }:
        evidence.append(
            DecisionEvidence.create(
                producer=ProcessingRole.RISK_ENGINE,
                evidence_type="DETERMINISTIC_RISK",
                verdict=(
                    EvidenceVerdict.APPROVE
                    if normalized_status
                    in {
                        DecisionStatus.RISK_APPROVED,
                        DecisionStatus.PAPER_APPROVED,
                        DecisionStatus.PAPER_FILLED,
                    }
                    else EvidenceVerdict.REJECT
                ),
                payload=normalized_risk_payload,
            )
        )
    settled = payload.get(
        "settled_candle_timestamp",
        payload.get("signal_candle_timestamp", payload.get("candle_timestamp")),
    )
    if settled is None:
        raise ValueError("Runtime Decision payload lacks a settled candle timestamp")
    return DecisionRecord.create(
        market=market,
        provider=provider,
        candidate_id=stable_candidate_id(market, payload),
        feature_snapshot_id=(str(payload.get("feature_snapshot_id", "")).strip() or None),
        symbol=str(payload.get("symbol", payload.get("instrument", ""))),
        timeframe=str(payload.get("timeframe", "")),
        side=str(payload.get("signal_type", payload.get("side", ""))),
        settled_candle_timestamp=settled,
        status=normalized_status,
        evidence=evidence,
        payload=payload,
        rejection_reasons=rejection_reasons,
        final_action=str(payload.get("final_action", "")) or None,
    )
