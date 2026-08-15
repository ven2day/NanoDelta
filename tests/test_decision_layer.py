from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.decisions import (
    DecisionEvidence,
    DecisionRecord,
    DecisionStatus,
    EvidenceVerdict,
    SchemaBoundDecisionRepository,
    decision_record_from_payload,
)
from src.core.pipeline import ProcessingRole


def _payload() -> dict[str, Any]:
    return {
        "candidate_id": "candidate-1",
        "feature_snapshot_id": "feature-1",
        "symbol": "RELIANCE",
        "timeframe": "15m",
        "signal_type": "BUY",
        "settled_candle_timestamp": "2026-08-15T12:00:00+00:00",
        "strategy": "momentum",
        "pipeline_eligible": True,
        "ml_status": "ABSTAINED",
        "validation": {"decision": "approve", "model_used": "qwen"},
        "risk_result": {"status": "APPROVED"},
    }


def test_runtime_payload_materializes_versioned_decision_evidence() -> None:
    record = decision_record_from_payload(
        market="NSE",
        provider="DHAN",
        payload=_payload(),
        status=DecisionStatus.PAPER_APPROVED,
    )

    assert record.status is DecisionStatus.PAPER_APPROVED
    assert record.final_action == "PAPER_BUY"
    assert record.feature_snapshot_id == "feature-1"
    assert {item.producer for item in record.evidence} == {
        ProcessingRole.STRATEGY_ENGINE,
        ProcessingRole.ML_INFERENCE,
        ProcessingRole.TRADING_AGENTS,
        ProcessingRole.RISK_ENGINE,
    }


def test_agents_and_ml_cannot_approve_decisions() -> None:
    with pytest.raises(ValueError, match="Only deterministic risk"):
        DecisionEvidence.create(
            producer=ProcessingRole.TRADING_AGENTS,
            evidence_type="AI_REVIEW",
            verdict=EvidenceVerdict.APPROVE,
        )


def test_approved_record_requires_risk_approval_evidence() -> None:
    strategy_evidence = DecisionEvidence.create(
        producer=ProcessingRole.STRATEGY_ENGINE,
        evidence_type="TECHNICAL",
        verdict=EvidenceVerdict.SUPPORT,
    )
    with pytest.raises(ValueError, match="require deterministic risk approval"):
        DecisionRecord.create(
            market="FOREX",
            provider="OANDA",
            candidate_id="candidate-1",
            feature_snapshot_id="feature-1",
            symbol="EUR_USD",
            timeframe="15m",
            side="SELL",
            settled_candle_timestamp="2026-08-15T12:00:00+00:00",
            status=DecisionStatus.PAPER_APPROVED,
            evidence=[strategy_evidence],
            payload={},
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


def test_decision_repository_upserts_and_rejects_cross_market() -> None:
    engine = _PostgresEngine()
    repository = SchemaBoundDecisionRepository(
        engine,  # type: ignore[arg-type]
        market="NSE",
        provider="DHAN",
    )
    record = decision_record_from_payload(
        market="NSE",
        provider="DHAN",
        payload=_payload(),
        status=DecisionStatus.PAPER_FILLED,
    )

    assert repository.persist_many([record]) == 1
    sql = "\n".join(call[0] for call in engine.connection.calls)
    assert 'INSERT INTO "nse"."decision_records"' in sql
    assert "ON CONFLICT (decision_id) DO UPDATE" in sql

    forex_payload = {**_payload(), "symbol": "EUR_USD", "signal_type": "SELL"}
    forex = decision_record_from_payload(
        market="FOREX",
        provider="OANDA",
        payload=forex_payload,
        status=DecisionStatus.REJECTED,
        rejection_reasons=("RISK_BLOCKED",),
    )
    with pytest.raises(ValueError, match="cannot write FOREX"):
        repository.persist_many([forex])
