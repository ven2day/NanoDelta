from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from nanodelta.agents import (
    AdvisoryAction,
    AgentRequest,
    ApprovedCandidate,
    TradingAgentsAdapter,
    TradingAgentsGraphBackend,
)
from nanodelta.contracts import Market
from nanodelta.strategies import StrategyApproval, StrategyIdentity


class FakeBackend:
    framework_version = "0.3.1@abc123"
    model_config = {"llm_provider": "test", "deep_think_llm": "deterministic-fake"}

    def __init__(self, decision: object = None, *, fail: bool = False) -> None:
        self.decision = decision or {"action": "BUY", "confidence": 0.8}
        self.fail = fail
        self.calls: list[tuple[str, object]] = []

    def analyze(self, symbol: str, analysis_date: object) -> tuple[dict[str, Any], object]:
        self.calls.append((symbol, analysis_date))
        if self.fail:
            raise TimeoutError("bounded timeout")
        return {
            "market_report": "RSI supports candidate",
            "news_report": "No citation",
        }, self.decision


def context() -> tuple[ApprovedCandidate, StrategyApproval, datetime]:
    now = datetime(2026, 8, 15, 12, tzinfo=UTC)
    identity = StrategyIdentity(Market.NSE, "vwap_pullback", "1.0.0", "5m", "30m", 1)
    approval = StrategyApproval.create(
        identity=identity,
        validation_run_id="validation-1",
        approved_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=30),
        approved_by="committee",
        reason="passed",
    )
    candidate = ApprovedCandidate(
        "candidate-1",
        identity,
        approval.approval_id,
        "RELIANCE",
        now,
        ("gold-1", "gold-2"),
        AdvisoryAction.BUY,
    )
    return candidate, approval, now


def test_adapter_requires_exact_current_approval_and_normalizes_evidence() -> None:
    candidate, approval, now = context()
    backend = FakeBackend()
    adapter = TradingAgentsAdapter(backend, now=lambda: now)

    result = adapter.run(AgentRequest.create(candidate, requested_at=now), approval)

    assert result.action is AdvisoryAction.BUY
    assert result.confidence == 0.8
    assert result.approval_id == approval.approval_id
    assert backend.calls[0][0] == "RELIANCE.NS"
    assert {role.role for role in result.roles} == {"technical_analyst", "news_analyst"}
    assert "gold-1" not in result.raw_decision


def test_adapter_caches_exact_input_and_configuration() -> None:
    candidate, approval, now = context()
    backend = FakeBackend()
    adapter = TradingAgentsAdapter(backend, now=lambda: now)
    request = AgentRequest.create(candidate, requested_at=now)

    first = adapter.run(request, approval)
    second = adapter.run(request, approval)

    assert second is first
    assert len(backend.calls) == 1


def test_adapter_failure_is_explicit_abstention_not_hidden_retry() -> None:
    candidate, approval, now = context()
    backend = FakeBackend(fail=True)

    result = TradingAgentsAdapter(backend, now=lambda: now).run(
        AgentRequest.create(candidate, requested_at=now), approval
    )

    assert result.action is AdvisoryAction.ABSTAIN
    assert result.error == "TimeoutError: bounded timeout"
    assert len(backend.calls) == 1


def test_adapter_rejects_mismatched_approval() -> None:
    candidate, approval, now = context()
    wrong = StrategyApproval.create(
        identity=approval.identity,
        validation_run_id="other",
        approved_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=1),
        approved_by="committee",
        reason="other",
    )
    with pytest.raises(PermissionError, match="approval_id"):
        TradingAgentsAdapter(FakeBackend(), now=lambda: now).run(
            AgentRequest.create(candidate, requested_at=now), wrong
        )


def test_graph_backend_uses_pinned_version_and_propagate_contract() -> None:
    calls: list[tuple[str, str]] = []

    class Graph:
        def __init__(self, **_: object) -> None:
            pass

        def propagate(self, symbol: str, analysis_date: str) -> tuple[dict[str, str], str]:
            calls.append((symbol, analysis_date))
            return {}, "SELL"

    backend = TradingAgentsGraphBackend(
        config={"llm_provider": "test"},
        framework_version="0.3.1@abc123",
        graph_factory=Graph,
    )
    _, decision = backend.analyze("BTC-USD", datetime(2026, 8, 15).date())

    assert decision == "SELL"
    assert calls == [("BTC-USD", "2026-08-15")]
