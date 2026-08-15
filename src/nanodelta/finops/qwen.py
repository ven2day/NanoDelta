"""OpenAI-compatible Qwen gateway guarded by NanoDelta FinOps."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from nanodelta.finops.core import Attribution, FinOpsGuard, TokenUsage


class QwenTransport(Protocol):
    async def complete(self, body: Mapping[str, Any], *, api_key: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class HttpxQwenTransport:
    endpoint: str
    timeout_seconds: float = 60

    async def complete(self, body: Mapping[str, Any], *, api_key: str) -> Mapping[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=dict(body),
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("Qwen response must be a JSON object")
        return payload


@dataclass
class QwenFinOpsGateway:
    guard: FinOpsGuard
    transport: QwenTransport
    api_key: str = field(repr=False)
    deployment_scope: str

    async def complete(
        self,
        body: Mapping[str, Any],
        *,
        attribution: Attribution,
        estimated_input_tokens: int,
    ) -> Mapping[str, Any]:
        if body.get("stream") is True:
            raise ValueError("streaming Qwen calls require a usage-aware stream adapter")
        model = str(body.get("model", "")).strip()
        if not model:
            raise ValueError("Qwen model is required")
        maximum_output = int(body.get("max_completion_tokens", body.get("max_tokens", 0)))
        if maximum_output < 1:
            raise ValueError(
                "max_completion_tokens is required for bounded Qwen FinOps authorization"
            )
        reservation = self.guard.authorize(
            model=model,
            deployment_scope=self.deployment_scope,
            estimated_input_tokens=estimated_input_tokens,
            maximum_output_tokens=maximum_output,
            now=datetime.now(UTC),
        )
        try:
            response = await self.transport.complete(body, api_key=self.api_key)
            usage = self.usage(response)
            request_id = str(response.get("id", "")).strip()
            if not request_id:
                raise ValueError("Qwen response is missing request ID")
            self.guard.record(
                reservation,
                provider_request_id=request_id,
                model=model,
                deployment_scope=self.deployment_scope,
                attribution=attribution,
                usage=usage,
                occurred_at=datetime.now(UTC),
            )
            return response
        except Exception:
            self.guard.cancel(reservation)
            raise

    @staticmethod
    def usage(response: Mapping[str, Any]) -> TokenUsage:
        raw = response.get("usage")
        if not isinstance(raw, Mapping):
            raise ValueError("Qwen response is missing token usage")
        prompt_details = raw.get("prompt_tokens_details")
        completion_details = raw.get("completion_tokens_details")
        cached = (
            int(prompt_details.get("cached_tokens", 0))
            if isinstance(prompt_details, Mapping)
            else 0
        )
        reasoning = (
            int(completion_details.get("reasoning_tokens", 0))
            if isinstance(completion_details, Mapping)
            else 0
        )
        return TokenUsage(
            int(raw.get("prompt_tokens", 0)),
            int(raw.get("completion_tokens", 0)),
            cached,
            reasoning,
        )
