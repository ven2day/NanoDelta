"""Provider-neutral FinOps contracts with explicit PAYG/subscription semantics."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from nanodelta.contracts import Market, stable_id, utc


class BillingMode(StrEnum):
    PAYG = "PAYG"
    SUBSCRIPTION = "SUBSCRIPTION"


@dataclass(frozen=True)
class Attribution:
    market: Market | None
    component: str
    reason: str

    def __post_init__(self) -> None:
        if not self.component.strip() or not self.reason.strip():
            raise ValueError("component and reason are required")


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0

    def __post_init__(self) -> None:
        values = (
            self.input_tokens,
            self.output_tokens,
            self.cached_input_tokens,
            self.reasoning_tokens,
        )
        if any(value < 0 for value in values):
            raise ValueError("token counts cannot be negative")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input cannot exceed total input")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class PriceTier:
    model: str
    deployment_scope: str
    maximum_input_tokens: int
    input_usd_per_million: Decimal
    cached_input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    catalog_version: str

    def __post_init__(self) -> None:
        if self.maximum_input_tokens < 1:
            raise ValueError("price tier token ceiling must be positive")
        if any(
            value < 0
            for value in (
                self.input_usd_per_million,
                self.cached_input_usd_per_million,
                self.output_usd_per_million,
            )
        ):
            raise ValueError("prices cannot be negative")

    def cost(self, usage: TokenUsage) -> Decimal:
        uncached = usage.input_tokens - usage.cached_input_tokens
        million = Decimal(1_000_000)
        return (
            Decimal(uncached) * self.input_usd_per_million
            + Decimal(usage.cached_input_tokens) * self.cached_input_usd_per_million
            + Decimal(usage.output_tokens) * self.output_usd_per_million
        ) / million


class PriceCatalog:
    def __init__(self, tiers: tuple[PriceTier, ...]) -> None:
        self._tiers = tiers

    def tier(self, model: str, deployment_scope: str, input_tokens: int) -> PriceTier:
        matches = sorted(
            (
                tier
                for tier in self._tiers
                if tier.model == model
                and tier.deployment_scope == deployment_scope
                and input_tokens <= tier.maximum_input_tokens
            ),
            key=lambda tier: tier.maximum_input_tokens,
        )
        if not matches:
            raise LookupError(
                f"no price tier for {model}/{deployment_scope}/{input_tokens} input tokens"
            )
        return matches[0]


@dataclass(frozen=True)
class SubscriptionPlan:
    plan_id: str
    monthly_fee_usd: Decimal
    five_hour_request_limit: int | None = None
    weekly_request_limit: int | None = None
    monthly_request_limit: int | None = None

    def __post_init__(self) -> None:
        if self.monthly_fee_usd < 0:
            raise ValueError("subscription fee cannot be negative")
        limits = (
            self.five_hour_request_limit,
            self.weekly_request_limit,
            self.monthly_request_limit,
        )
        if any(value is not None and value < 1 for value in limits):
            raise ValueError("subscription request limits must be positive")


@dataclass(frozen=True)
class BudgetPolicy:
    daily_request_limit: int
    daily_token_limit: int
    daily_cost_limit_usd: Decimal
    alert_thresholds: tuple[Decimal, ...] = (
        Decimal("0.50"),
        Decimal("0.80"),
        Decimal("1.00"),
    )

    def __post_init__(self) -> None:
        if self.daily_request_limit < 1 or self.daily_token_limit < 1:
            raise ValueError("daily request and token limits must be positive")
        if self.daily_cost_limit_usd < 0:
            raise ValueError("daily cost limit cannot be negative")
        if any(not Decimal(0) < value <= Decimal(1) for value in self.alert_thresholds):
            raise ValueError("alert thresholds must be in (0, 1]")


@dataclass(frozen=True)
class UsageRecord:
    record_id: str
    provider_request_id: str
    model: str
    deployment_scope: str
    billing_mode: BillingMode
    attribution: Attribution
    usage: TokenUsage
    marginal_cost_usd: Decimal
    catalog_version: str | None
    occurred_at: datetime


@dataclass(frozen=True)
class FinOpsAlert:
    alert_id: str
    alert_type: str
    threshold: Decimal
    observed: Decimal
    occurred_at: datetime


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    estimated_tokens: int
    estimated_cost_usd: Decimal
    created_at: datetime


@dataclass(frozen=True)
class DailyUsage:
    requests: int
    tokens: int
    marginal_cost_usd: Decimal


class InMemoryFinOpsLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.records: dict[str, UsageRecord] = {}
        self.reservations: dict[str, Reservation] = {}
        self.alerts: dict[str, FinOpsAlert] = {}
        self.kill_switch = False
        self.kill_reason: str | None = None

    def daily(self, day: date) -> DailyUsage:
        records = [record for record in self.records.values() if record.occurred_at.date() == day]
        return DailyUsage(
            len(records),
            sum(record.usage.total_tokens for record in records),
            sum((record.marginal_cost_usd for record in records), Decimal(0)),
        )

    def requests_since(self, since: datetime) -> int:
        return sum(record.occurred_at >= since for record in self.records.values())

    def reserved(self) -> tuple[int, int, Decimal]:
        return (
            len(self.reservations),
            sum(value.estimated_tokens for value in self.reservations.values()),
            sum((value.estimated_cost_usd for value in self.reservations.values()), Decimal(0)),
        )


class FinOpsGuard:
    def __init__(
        self,
        *,
        provider: str,
        billing_mode: BillingMode,
        policy: BudgetPolicy,
        ledger: InMemoryFinOpsLedger,
        price_catalog: PriceCatalog | None = None,
        subscription: SubscriptionPlan | None = None,
    ) -> None:
        if billing_mode is BillingMode.PAYG and price_catalog is None:
            raise ValueError("PAYG mode requires a price catalog")
        if billing_mode is BillingMode.SUBSCRIPTION and subscription is None:
            raise ValueError("subscription mode requires a subscription plan")
        self.provider = provider
        self.billing_mode = billing_mode
        self.policy = policy
        self.ledger = ledger
        self.price_catalog = price_catalog
        self.subscription = subscription

    def authorize(
        self,
        *,
        model: str,
        deployment_scope: str,
        estimated_input_tokens: int,
        maximum_output_tokens: int,
        now: datetime,
    ) -> Reservation:
        now = utc(now, "now")
        if min(estimated_input_tokens, maximum_output_tokens) < 0:
            raise ValueError("estimated token counts cannot be negative")
        with self.ledger._lock:
            if self.ledger.kill_switch:
                raise PermissionError(f"LLM spend kill-switch active: {self.ledger.kill_reason}")
            estimated_usage = TokenUsage(estimated_input_tokens, maximum_output_tokens)
            estimated_cost = self._cost(model, deployment_scope, estimated_usage)[0]
            daily = self.ledger.daily(now.date())
            reserved_requests, reserved_tokens, reserved_cost = self.ledger.reserved()
            projected = DailyUsage(
                daily.requests + reserved_requests + 1,
                daily.tokens + reserved_tokens + estimated_usage.total_tokens,
                daily.marginal_cost_usd + reserved_cost + estimated_cost,
            )
            self._enforce_subscription(now)
            reason = self._budget_reason(projected)
            if reason is not None:
                self._kill(reason, now)
                raise PermissionError(reason)
            reservation = Reservation(
                stable_id(model, deployment_scope, now.isoformat(), projected.requests),
                estimated_usage.total_tokens,
                estimated_cost,
                now,
            )
            self.ledger.reservations[reservation.reservation_id] = reservation
            return reservation

    def record(
        self,
        reservation: Reservation,
        *,
        provider_request_id: str,
        model: str,
        deployment_scope: str,
        attribution: Attribution,
        usage: TokenUsage,
        occurred_at: datetime,
    ) -> UsageRecord:
        occurred_at = utc(occurred_at, "occurred_at")
        with self.ledger._lock:
            if self.ledger.reservations.pop(reservation.reservation_id, None) is None:
                raise ValueError("unknown or already completed FinOps reservation")
            cost, catalog_version = self._cost(model, deployment_scope, usage)
            record = UsageRecord(
                stable_id(self.provider, provider_request_id),
                provider_request_id,
                model,
                deployment_scope,
                self.billing_mode,
                attribution,
                usage,
                cost,
                catalog_version,
                occurred_at,
            )
            existing = self.ledger.records.get(record.record_id)
            if existing is not None:
                if existing != record:
                    raise ValueError("provider request ID is bound to different usage")
                return existing
            self.ledger.records[record.record_id] = record
            self._alerts_and_postcheck(occurred_at)
            return record

    def cancel(self, reservation: Reservation) -> None:
        with self.ledger._lock:
            self.ledger.reservations.pop(reservation.reservation_id, None)

    def set_kill_switch(self, active: bool, *, reason: str, now: datetime) -> None:
        with self.ledger._lock:
            self.ledger.kill_switch = active
            self.ledger.kill_reason = reason if active else None
            if active:
                self._add_alert("MANUAL_KILL_SWITCH", Decimal(1), Decimal(1), utc(now, "now"))

    def _cost(self, model: str, scope: str, usage: TokenUsage) -> tuple[Decimal, str | None]:
        if self.billing_mode is BillingMode.SUBSCRIPTION:
            return Decimal(0), None
        assert self.price_catalog is not None
        tier = self.price_catalog.tier(model, scope, usage.input_tokens)
        return tier.cost(usage), tier.catalog_version

    def _enforce_subscription(self, now: datetime) -> None:
        if self.subscription is None:
            return
        checks = (
            (timedelta(hours=5), self.subscription.five_hour_request_limit, "5-hour"),
            (timedelta(days=7), self.subscription.weekly_request_limit, "weekly"),
            (timedelta(days=31), self.subscription.monthly_request_limit, "monthly"),
        )
        for window, limit, label in checks:
            projected = self.ledger.requests_since(now - window) + len(self.ledger.reservations) + 1
            if limit is not None and projected > limit:
                reason = f"Qwen subscription {label} request quota reached"
                self._kill(reason, now)
                raise PermissionError(reason)

    def _budget_reason(self, usage: DailyUsage) -> str | None:
        if usage.requests > self.policy.daily_request_limit:
            return "daily LLM request budget exceeded"
        if usage.tokens > self.policy.daily_token_limit:
            return "daily LLM token budget exceeded"
        if usage.marginal_cost_usd > self.policy.daily_cost_limit_usd:
            return "daily LLM marginal-cost budget exceeded"
        return None

    def _alerts_and_postcheck(self, now: datetime) -> None:
        daily = self.ledger.daily(now.date())
        ratios = {
            "REQUEST_BUDGET": Decimal(daily.requests) / self.policy.daily_request_limit,
            "TOKEN_BUDGET": Decimal(daily.tokens) / self.policy.daily_token_limit,
            "COST_BUDGET": (
                daily.marginal_cost_usd / self.policy.daily_cost_limit_usd
                if self.policy.daily_cost_limit_usd > 0
                else Decimal(0)
            ),
        }
        for alert_type, ratio in ratios.items():
            for threshold in self.policy.alert_thresholds:
                if ratio >= threshold:
                    self._add_alert(alert_type, threshold, ratio, now)
        reason = self._budget_reason(daily)
        if reason is not None:
            self._kill(reason, now)

    def _kill(self, reason: str, now: datetime) -> None:
        self.ledger.kill_switch = True
        self.ledger.kill_reason = reason
        self._add_alert("KILL_SWITCH", Decimal(1), Decimal(1), now)

    def _add_alert(
        self, alert_type: str, threshold: Decimal, observed: Decimal, now: datetime
    ) -> None:
        alert = FinOpsAlert(
            stable_id(alert_type, threshold, now.date()),
            alert_type,
            threshold,
            observed,
            now,
        )
        self.ledger.alerts.setdefault(alert.alert_id, alert)
