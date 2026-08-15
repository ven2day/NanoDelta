"""Bounded adapter for TauricResearch/TradingAgents.

The upstream graph is treated as an untrusted, non-deterministic research backend. This
module normalizes its output into immutable evidence; it exposes no broker, sizing, approval,
or persistence capability.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Protocol

from nanodelta.contracts import Market, stable_id, utc
from nanodelta.strategies.registry import StrategyApproval, StrategyIdentity


class AdvisoryAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    ABSTAIN = "ABSTAIN"


@dataclass(frozen=True)
class ApprovedCandidate:
    candidate_id: str
    identity: StrategyIdentity
    approval_id: str
    symbol: str
    event_time: datetime
    gold_snapshot_ids: tuple[str, ...]
    deterministic_action: AdvisoryAction

    def __post_init__(self) -> None:
        if self.deterministic_action is AdvisoryAction.ABSTAIN:
            raise ValueError("only a deterministic BUY or SELL candidate can be researched")
        if not self.symbol or not self.gold_snapshot_ids:
            raise ValueError("symbol and gold_snapshot_ids are required")
        utc(self.event_time, "event_time")


@dataclass(frozen=True)
class AgentRequest:
    candidate: ApprovedCandidate
    requested_at: datetime
    input_fingerprint: str

    @classmethod
    def create(cls, candidate: ApprovedCandidate, *, requested_at: datetime) -> AgentRequest:
        requested_at = utc(requested_at, "requested_at")
        fingerprint = stable_id(
            candidate.candidate_id,
            candidate.approval_id,
            *candidate.gold_snapshot_ids,
            utc(candidate.event_time, "event_time").isoformat(),
        )
        return cls(candidate, requested_at, fingerprint)


@dataclass(frozen=True)
class RoleEvidence:
    role: str
    summary: str


@dataclass(frozen=True)
class AgentEvidence:
    evidence_id: str
    cache_key: str
    input_fingerprint: str
    candidate_id: str
    approval_id: str
    framework: str
    framework_version: str
    model_config: tuple[tuple[str, str], ...]
    started_at: datetime
    completed_at: datetime
    action: AdvisoryAction
    confidence: float | None
    roles: tuple[RoleEvidence, ...]
    raw_decision: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["action"] = self.action.value
        result["started_at"] = self.started_at.isoformat()
        result["completed_at"] = self.completed_at.isoformat()
        return result


class AdvisoryBackend(Protocol):
    framework_version: str
    model_config: Mapping[str, str]

    def analyze(self, symbol: str, analysis_date: date) -> tuple[Mapping[str, Any], object]: ...


class TradingAgentsGraphBackend:
    """Lazy wrapper around the optional upstream TradingAgents package."""

    def __init__(
        self,
        *,
        config: Mapping[str, Any],
        framework_version: str,
        graph_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._config = dict(config)
        self.framework_version = framework_version
        self.model_config = {
            key: str(value)
            for key, value in self._config.items()
            if key in {"llm_provider", "deep_think_llm", "quick_think_llm", "temperature"}
        }
        self._graph_factory = graph_factory

    def analyze(self, symbol: str, analysis_date: date) -> tuple[Mapping[str, Any], object]:
        factory = self._graph_factory
        if factory is None:
            try:
                from tradingagents.graph.trading_graph import (  # type: ignore[import-not-found]
                    TradingAgentsGraph,
                )
            except ImportError as exc:
                raise RuntimeError(
                    "TradingAgents is optional; install the pinned external package"
                ) from exc
            factory = TradingAgentsGraph
        graph = factory(debug=False, config=self._config.copy())
        state, decision = graph.propagate(symbol, analysis_date.isoformat())
        if not isinstance(state, Mapping):
            state = {}
        return state, decision


class TradingAgentsAdapter:
    """Normalizes advisory output without granting it execution authority."""

    _ROLE_FIELDS = {
        "technical_analyst": "market_report",
        "sentiment_analyst": "sentiment_report",
        "news_analyst": "news_report",
        "fundamentals_analyst": "fundamentals_report",
        "bull_researcher": "investment_debate_state",
        "candidate_reviewer": "trader_investment_plan",
        "risk_review": "risk_debate_state",
    }

    def __init__(
        self,
        backend: AdvisoryBackend,
        *,
        symbol_resolver: Callable[[Market, str], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._backend = backend
        self._symbol_resolver = symbol_resolver or self._default_symbol
        self._now = now or (lambda: datetime.now(UTC))
        self._cache: dict[str, AgentEvidence] = {}

    def run(self, request: AgentRequest, approval: StrategyApproval) -> AgentEvidence:
        candidate = request.candidate
        if approval.approval_id != candidate.approval_id:
            raise PermissionError("candidate approval_id does not match supplied approval")
        if approval.identity != candidate.identity:
            raise PermissionError("candidate identity does not match supplied approval")
        if not approval.is_current(request.requested_at):
            raise PermissionError("TradingAgents requires a current exact-identity approval")

        cache_key = stable_id(
            request.input_fingerprint,
            self._backend.framework_version,
            tuple(sorted(self._backend.model_config.items())),
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        started_at = self._now().astimezone(UTC)
        symbol = self._symbol_resolver(candidate.identity.market, candidate.symbol)
        try:
            state, decision = self._backend.analyze(symbol, candidate.event_time.date())
            raw_decision = self._serialize(decision)
            action, confidence = self._normalize_decision(decision)
            roles = self._extract_roles(state)
            error = None
        except Exception as exc:
            raw_decision = ""
            action = AdvisoryAction.ABSTAIN
            confidence = None
            roles = ()
            error = f"{type(exc).__name__}: {exc}"
        completed_at = self._now().astimezone(UTC)
        evidence = AgentEvidence(
            evidence_id=cache_key,
            cache_key=cache_key,
            input_fingerprint=request.input_fingerprint,
            candidate_id=candidate.candidate_id,
            approval_id=candidate.approval_id,
            framework="TauricResearch/TradingAgents",
            framework_version=self._backend.framework_version,
            model_config=tuple(sorted(self._backend.model_config.items())),
            started_at=started_at,
            completed_at=completed_at,
            action=action,
            confidence=confidence,
            roles=roles,
            raw_decision=raw_decision,
            error=error,
        )
        self._cache[cache_key] = evidence
        return evidence

    @classmethod
    def _extract_roles(cls, state: Mapping[str, Any]) -> tuple[RoleEvidence, ...]:
        evidence = []
        for role, field in cls._ROLE_FIELDS.items():
            value = state.get(field)
            if value:
                evidence.append(RoleEvidence(role, cls._serialize(value)))
        return tuple(evidence)

    @staticmethod
    def _normalize_decision(decision: object) -> tuple[AdvisoryAction, float | None]:
        confidence = None
        if isinstance(decision, Mapping):
            raw_action = str(
                decision.get("action", decision.get("decision", decision.get("rating", "")))
            )
            raw_confidence = decision.get("confidence")
            if isinstance(raw_confidence, (int, float)) and 0 <= float(raw_confidence) <= 1:
                confidence = float(raw_confidence)
        else:
            raw_action = str(decision)
        normalized = raw_action.strip().upper()
        if normalized in {"BUY", "STRONG BUY"}:
            return AdvisoryAction.BUY, confidence
        if normalized in {"SELL", "STRONG SELL"}:
            return AdvisoryAction.SELL, confidence
        return AdvisoryAction.ABSTAIN, confidence

    @staticmethod
    def _serialize(value: object) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def _default_symbol(market: Market, symbol: str) -> str:
        if market is Market.NSE and not symbol.endswith(".NS"):
            return f"{symbol}.NS"
        if market is Market.CRYPTO:
            return symbol.replace("_", "-")
        return symbol
