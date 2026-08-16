"""Application service for NSE validation and explicit, reviewed paper admission."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from nanodelta.strategies import StrategyApproval, StrategyPlugin, StrategyRegistry
from nanodelta.validation.nse import (
    NseStrategyEvidence,
    NseValidationCampaign,
    ResearchState,
    evaluate_nse_readiness,
    evaluate_nse_strategy,
)
from nanodelta.validation.postgres import PostgresNseValidationStore


class NseValidationService:
    def __init__(
        self,
        *,
        store: PostgresNseValidationStore,
        registry: StrategyRegistry,
    ) -> None:
        self._store = store
        self._registry = registry

    def validate(
        self,
        campaign: NseValidationCampaign,
        plugins: Sequence[StrategyPlugin],
    ) -> tuple[NseStrategyEvidence, ...]:
        """Persist research evidence only; validation never creates an approval."""
        candles = self._store.load_candles(
            symbols=campaign.symbols,
            timeframes=campaign.config.required_timeframes,
            start=campaign.requested_start - timedelta(days=30),
            end=campaign.requested_end,
        )
        self._store.record_campaign(campaign)
        readiness = evaluate_nse_readiness(campaign, candles)
        self._store.record_readiness(readiness)
        evidence: list[NseStrategyEvidence] = []
        for plugin in plugins:
            self._registry.register(plugin.definition)
            item = evaluate_nse_strategy(campaign, plugin, candles, readiness)
            self._registry.record_validation(item.validation)
            self._store.record_strategy_evidence(item)
            evidence.append(item)
        return tuple(evidence)

    def promote(
        self,
        *,
        evidence_id: str,
        reviewed_by: str,
        reason: str,
        approved_at: datetime,
        expires_at: datetime,
    ) -> StrategyApproval:
        """Explicit operator action; failed or incomplete research cannot be promoted."""
        if not reviewed_by.strip() or not reason.strip():
            raise ValueError("reviewed_by and reason are required")
        target = self._store.promotion_target(evidence_id)
        if target.state is not ResearchState.RESEARCH or not target.passed:
            raise PermissionError("only passing RESEARCH evidence can be paper-approved")
        approval = StrategyApproval.create(
            identity=target.identity,
            validation_run_id=target.validation_run_id,
            approved_at=approved_at,
            expires_at=expires_at,
            approved_by=reviewed_by,
            reason=reason,
        )
        self._registry.record_approval(approval)
        self._store.record_promotion(
            evidence_id=evidence_id,
            approval_id=approval.approval_id,
            reviewed_by=reviewed_by,
            reason=reason,
            promoted_at=approved_at,
        )
        return approval

    def strategies(
        self,
        *,
        strategy_id: str | None = None,
        timeframe: str | None = None,
        lifecycle_state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[dict[str, object], ...]:
        self._page(limit, offset)
        return self._store.strategies(
            strategy_id=strategy_id,
            timeframe=timeframe,
            lifecycle_state=lifecycle_state,
            limit=limit,
            offset=offset,
        )

    def backtests(
        self,
        *,
        strategy_id: str | None = None,
        timeframe: str | None = None,
        research_state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[dict[str, object], ...]:
        self._page(limit, offset)
        return self._store.backtests(
            strategy_id=strategy_id,
            timeframe=timeframe,
            research_state=research_state,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    def _page(limit: int, offset: int) -> None:
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("limit must be 1..500 and offset must be non-negative")
