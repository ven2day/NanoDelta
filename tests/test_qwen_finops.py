from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from nanodelta.contracts import Market
from nanodelta.finops import (
    Attribution,
    BillingMode,
    BudgetPolicy,
    FinOpsGuard,
    InMemoryFinOpsLedger,
    PriceCatalog,
    PriceTier,
    QwenFinOpsGateway,
    SubscriptionPlan,
)

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)


def policy(*, requests: int = 10, tokens: int = 10_000, cost: str = "1") -> BudgetPolicy:
    return BudgetPolicy(requests, tokens, Decimal(cost))


def catalog() -> PriceCatalog:
    return PriceCatalog(
        (
            PriceTier(
                "qwen-test",
                "international",
                32_000,
                Decimal("1"),
                Decimal("0.1"),
                Decimal("2"),
                "official-2026-08-15",
            ),
        )
    )


def payg_guard(**kwargs: Any) -> FinOpsGuard:
    return FinOpsGuard(
        provider="qwen",
        billing_mode=BillingMode.PAYG,
        policy=kwargs.get("policy", policy()),
        ledger=kwargs.get("ledger", InMemoryFinOpsLedger()),
        price_catalog=catalog(),
    )


def test_payg_cost_uses_uncached_cached_and_output_prices() -> None:
    guard = payg_guard()
    reservation = guard.authorize(
        model="qwen-test",
        deployment_scope="international",
        estimated_input_tokens=1_000,
        maximum_output_tokens=100,
        now=NOW,
    )
    from nanodelta.finops.core import TokenUsage

    record = guard.record(
        reservation,
        provider_request_id="request-1",
        model="qwen-test",
        deployment_scope="international",
        attribution=Attribution(Market.NSE, "tradingagents", "candidate_review"),
        usage=TokenUsage(1_000, 100, cached_input_tokens=200),
        occurred_at=NOW,
    )

    assert record.marginal_cost_usd == Decimal("0.00102")
    assert record.catalog_version == "official-2026-08-15"
    assert guard.ledger.daily(NOW.date()).tokens == 1_100


def test_preflight_budget_exceed_activates_kill_switch() -> None:
    guard = payg_guard(policy=policy(tokens=100))

    with pytest.raises(PermissionError, match="token budget"):
        guard.authorize(
            model="qwen-test",
            deployment_scope="international",
            estimated_input_tokens=80,
            maximum_output_tokens=30,
            now=NOW,
        )

    assert guard.ledger.kill_switch
    with pytest.raises(PermissionError, match="kill-switch active"):
        guard.authorize(
            model="qwen-test",
            deployment_scope="international",
            estimated_input_tokens=1,
            maximum_output_tokens=1,
            now=NOW,
        )


def test_subscription_tracks_tokens_but_has_zero_marginal_token_cost() -> None:
    guard = FinOpsGuard(
        provider="qwen",
        billing_mode=BillingMode.SUBSCRIPTION,
        policy=policy(),
        ledger=InMemoryFinOpsLedger(),
        subscription=SubscriptionPlan("coding-plan", Decimal("50"), five_hour_request_limit=1),
    )
    reservation = guard.authorize(
        model="qwen3-coder-plus",
        deployment_scope="international",
        estimated_input_tokens=10,
        maximum_output_tokens=10,
        now=NOW,
    )
    from nanodelta.finops.core import TokenUsage

    record = guard.record(
        reservation,
        provider_request_id="subscription-1",
        model="qwen3-coder-plus",
        deployment_scope="international",
        attribution=Attribution(None, "coding", "maintenance"),
        usage=TokenUsage(8, 4),
        occurred_at=NOW,
    )
    assert record.marginal_cost_usd == 0

    with pytest.raises(PermissionError, match="5-hour"):
        guard.authorize(
            model="qwen3-coder-plus",
            deployment_scope="international",
            estimated_input_tokens=1,
            maximum_output_tokens=1,
            now=NOW,
        )


class FakeQwenTransport:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def complete(self, body: Any, *, api_key: str) -> dict[str, Any]:
        self.keys.append(api_key)
        return {
            "id": "chatcmpl-1",
            "model": body["model"],
            "choices": [{"message": {"role": "assistant", "content": "BUY"}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 25},
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
        }


@pytest.mark.asyncio
async def test_qwen_gateway_records_official_usage_shape_and_hides_key() -> None:
    guard = payg_guard()
    transport = FakeQwenTransport()
    gateway = QwenFinOpsGateway(guard, transport, "secret-key", "international")

    response = await gateway.complete(
        {"model": "qwen-test", "max_completion_tokens": 50},
        attribution=Attribution(Market.FOREX, "tradingagents", "risk_review"),
        estimated_input_tokens=100,
    )

    assert response["id"] == "chatcmpl-1"
    record = next(iter(guard.ledger.records.values()))
    assert record.usage.cached_input_tokens == 25
    assert record.usage.reasoning_tokens == 5
    assert transport.keys == ["secret-key"]
    assert "secret-key" not in repr(gateway)


@pytest.mark.asyncio
async def test_qwen_gateway_rejects_streaming_until_usage_aware_adapter_exists() -> None:
    gateway = QwenFinOpsGateway(payg_guard(), FakeQwenTransport(), "secret", "international")
    with pytest.raises(ValueError, match="usage-aware stream"):
        await gateway.complete(
            {"model": "qwen-test", "stream": True, "max_completion_tokens": 10},
            attribution=Attribution(Market.CRYPTO, "agent", "review"),
            estimated_input_tokens=10,
        )
