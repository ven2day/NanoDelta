from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from nanodelta.agents import AdvisoryAction
from nanodelta.contracts import Market
from nanodelta.orchestration.decision_pipeline import LlmVerdict, ScoreBreakdown, ScoredCandidate
from nanodelta.runtime.llm_review import QwenCandidateReviewer
from nanodelta.strategies import (
    DeterministicCandidate,
    StrategyContext,
    StrategyDefinition,
    StrategyIdentity,
    StrategySignal,
)

NOW = datetime(2026, 8, 17, 10, tzinfo=UTC)


def scored_candidate() -> ScoredCandidate:
    identity = StrategyIdentity(Market.NSE, "vwap_pullback", "1.0.0", "15m", "intraday", 1)
    definition = StrategyDefinition(identity, "trend", (), "tests:Fixed")
    context = StrategyContext(
        Market.NSE, "RELIANCE", "ENERGY", "15m", "intraday", 1, NOW, ("gold-1",), {"close": 100.0}
    )
    signal = StrategySignal(AdvisoryAction.BUY, 0.8, 100.0, 98.0, 104.0)
    candidate = DeterministicCandidate.create(definition, "approval-1", context, signal)
    score = ScoreBreakdown(0.8, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.05, 0.75)
    return ScoredCandidate(candidate, score)


class RaisingGateway:
    async def complete(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("network exploded")


class HangingGateway:
    async def complete(self, *args: object, **kwargs: object) -> dict[str, object]:
        await asyncio.sleep(10)
        return {}


class PassGateway:
    async def complete(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {"choices": [{"message": {"content": "PASS"}}]}


def test_gateway_exception_degrades_to_unavailable() -> None:
    reviewer = QwenCandidateReviewer(RaisingGateway(), model="qwen-test")  # type: ignore[arg-type]
    assert reviewer.review(scored_candidate()) is LlmVerdict.UNAVAILABLE


def test_gateway_timeout_degrades_to_unavailable() -> None:
    reviewer = QwenCandidateReviewer(
        HangingGateway(), model="qwen-test", timeout_seconds=0.05  # type: ignore[arg-type]
    )
    assert reviewer.review(scored_candidate()) is LlmVerdict.UNAVAILABLE


def test_pass_response_is_parsed() -> None:
    reviewer = QwenCandidateReviewer(PassGateway(), model="qwen-test")  # type: ignore[arg-type]
    assert reviewer.review(scored_candidate()) is LlmVerdict.PASS
