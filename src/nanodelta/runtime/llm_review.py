"""Qwen-backed candidate review -- pipeline stage 10, shadow mode only.

Wired at LlmReviewMode.SHADOW: StagedDecisionPipeline._review records the
verdict for observability but never rejects a candidate on it -- only
ENFORCED_VETO + BLOCK does that, and this deployment never sets ENFORCED_VETO.
Any failure here -- network, auth, budget exhausted, timeout, malformed
response -- degrades to LlmVerdict.UNAVAILABLE rather than raising, because an
exception from an optional review stage must never crash a live paper-decision
cycle that would otherwise have gone through fine without it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

from nanodelta.finops import Attribution, QwenFinOpsGateway
from nanodelta.orchestration.decision_pipeline import LlmVerdict, ScoredCandidate

logger = logging.getLogger("nanodelta.runtime.llm_review")

T = TypeVar("T")


def _run_sync(coroutine: Coroutine[object, object, T]) -> T:
    """Same sync-bridge pattern as api/runtime.py's _run_sync -- the decision
    pipeline's review step is synchronous, but the Qwen gateway is async. A
    dedicated thread gets its own fresh event loop so this can't collide with
    whatever loop (if any) the caller is already running inside."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


class QwenCandidateReviewer:
    def __init__(
        self,
        gateway: QwenFinOpsGateway,
        *,
        model: str,
        max_output_tokens: int = 16,
        timeout_seconds: float = 8.0,
    ) -> None:
        self._gateway = gateway
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._timeout = timeout_seconds

    def review(self, candidate: ScoredCandidate) -> LlmVerdict:
        try:
            return _run_sync(asyncio.wait_for(self._review_async(candidate), self._timeout))
        except Exception:
            logger.exception(
                "qwen candidate review failed",
                extra={"candidate_id": candidate.candidate.candidate_id},
            )
            return LlmVerdict.UNAVAILABLE

    async def _review_async(self, candidate: ScoredCandidate) -> LlmVerdict:
        signal = candidate.candidate.signal
        prompt = (
            f"NSE paper-trading candidate: symbol {candidate.candidate.symbol}, "
            f"proposed {signal.action.value} at {signal.reference_price}, "
            f"stop {signal.stop_price}, target {signal.target_price}, "
            f"strategy confidence {signal.confidence:.2f}, "
            f"expected_r {candidate.score.expected_r_net_of_costs:.3f}. "
            "Reply with exactly one word: PASS, BLOCK, or ABSTAIN."
        )
        body = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_completion_tokens": self._max_output_tokens,
        }
        response = await self._gateway.complete(
            body,
            attribution=Attribution(
                candidate.candidate.identity.market, "paper-decision-review", "shadow-review"
            ),
            estimated_input_tokens=200,
        )
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            return LlmVerdict.UNAVAILABLE
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = str(message.get("content", "")).strip().upper()
        if "BLOCK" in content:
            return LlmVerdict.BLOCK
        if "ABSTAIN" in content:
            return LlmVerdict.ABSTAIN
        if "PASS" in content:
            return LlmVerdict.PASS
        return LlmVerdict.UNAVAILABLE
