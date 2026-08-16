"""PostgreSQL persistence and fixed authoritative reads for NSE validation evidence."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, cast

from nanodelta.contracts import Market, Provider, stable_id
from nanodelta.persistence.migrations import Connection
from nanodelta.strategies import StrategyIdentity
from nanodelta.validation.nse import (
    NseReadinessEvidence,
    NseStrategyEvidence,
    NseValidationCampaign,
    ResearchState,
    SettledCandle,
)


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _campaign_config(campaign: NseValidationCampaign) -> dict[str, object]:
    config = campaign.config
    return {
        "minimum_history_days": config.minimum_history_days,
        "required_timeframes": list(config.required_timeframes),
        "walk_forward_windows": config.walk_forward_windows,
        "minimum_session_coverage": config.minimum_session_coverage,
        "maximum_last_bar_age_seconds": config.maximum_last_bar_age.total_seconds(),
        "tested_hypotheses": config.tested_hypotheses,
        "cost_model": asdict(config.cost_model),
        "policy": asdict(config.policy),
    }


@dataclass(frozen=True)
class PromotionTarget:
    evidence_id: str
    validation_run_id: str
    identity: StrategyIdentity
    state: ResearchState
    passed: bool


class PostgresNseValidationStore:
    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def load_candles(
        self,
        *,
        symbols: Sequence[str],
        timeframes: Sequence[str],
        start: datetime,
        end: datetime,
    ) -> tuple[SettledCandle, ...]:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT symbol,timeframe,open_time,open,high,low,close,volume,provider "
                "FROM nse_silver.candles WHERE symbol=ANY(%s) AND timeframe=ANY(%s) "
                "AND open_time>=%s AND open_time<=%s AND is_settled=true AND provider=%s "
                "ORDER BY symbol,timeframe,open_time",
                (list(symbols), list(timeframes), start, end, Provider.DHAN.value),
            )
            return tuple(self._candle(row) for row in cursor.fetchall())
        finally:
            connection.close()

    def record_campaign(self, campaign: NseValidationCampaign) -> None:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO research.nse_validation_campaigns "
                "(campaign_id,evaluated_at,requested_start,requested_end,minimum_history_days,"
                "source_provider,symbols,required_timeframes,validation_config) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb) "
                "ON CONFLICT (campaign_id) DO NOTHING",
                (
                    campaign.campaign_id,
                    campaign.evaluated_at,
                    campaign.requested_start,
                    campaign.requested_end,
                    campaign.config.minimum_history_days,
                    campaign.source_provider.value,
                    _dump(list(campaign.symbols)),
                    _dump(list(campaign.config.required_timeframes)),
                    _dump(_campaign_config(campaign)),
                ),
            )
            cursor.execute(
                "SELECT evaluated_at,requested_start,requested_end,minimum_history_days,"
                "source_provider FROM research.nse_validation_campaigns WHERE campaign_id=%s",
                (campaign.campaign_id,),
            )
            row = cursor.fetchone()
            expected = (
                campaign.evaluated_at,
                campaign.requested_start,
                campaign.requested_end,
                campaign.config.minimum_history_days,
                campaign.source_provider.value,
            )
            if row != expected:
                raise ValueError("NSE validation campaigns are immutable")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_readiness(self, evidence: Sequence[NseReadinessEvidence]) -> None:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            for item in evidence:
                cursor.execute(
                    "INSERT INTO research.nse_validation_readiness "
                    "(readiness_id,campaign_id,symbol,timeframe,first_open,last_open,settled_count,"
                    "minimum_settled_count,history_days,ready,reasons,source_fingerprint) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) "
                    "ON CONFLICT (readiness_id) DO NOTHING",
                    (
                        item.readiness_id,
                        item.campaign_id,
                        item.symbol,
                        item.timeframe,
                        item.first_open,
                        item.last_open,
                        item.settled_count,
                        item.minimum_settled_count,
                        item.history_days,
                        item.ready,
                        _dump(list(item.reasons)),
                        item.source_fingerprint,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_strategy_evidence(self, evidence: NseStrategyEvidence) -> None:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO research.nse_strategy_evidence "
                "(evidence_id,campaign_id,validation_run_id,strategy_key,research_state,"
                "data_fingerprint,walk_forward_windows,cost_model,stressed_net_expectancy) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s) "
                "ON CONFLICT (evidence_id) DO NOTHING",
                (
                    evidence.evidence_id,
                    evidence.campaign_id,
                    evidence.validation.validation_run_id,
                    evidence.validation.identity.key,
                    evidence.state.value,
                    evidence.data_fingerprint,
                    _dump([asdict(window) for window in evidence.windows]),
                    _dump(asdict(evidence.cost_model)),
                    evidence.stressed_net_expectancy,
                ),
            )
            cursor.execute(
                "SELECT campaign_id,validation_run_id,strategy_key,research_state,"
                "data_fingerprint FROM research.nse_strategy_evidence WHERE evidence_id=%s",
                (evidence.evidence_id,),
            )
            row = cursor.fetchone()
            expected = (
                evidence.campaign_id,
                evidence.validation.validation_run_id,
                evidence.validation.identity.key,
                evidence.state.value,
                evidence.data_fingerprint,
            )
            if row != expected:
                raise ValueError("NSE strategy evidence is immutable")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def promotion_target(self, evidence_id: str) -> PromotionTarget:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT e.evidence_id,e.validation_run_id,d.market,d.strategy_id,"
                "d.strategy_version,d.timeframe,d.trade_horizon,d.feature_set_version,"
                "e.research_state,v.passed FROM research.nse_strategy_evidence e "
                "JOIN research.validation_runs v ON v.validation_run_id=e.validation_run_id "
                "JOIN research.strategy_definitions d ON d.strategy_key=e.strategy_key "
                "WHERE e.evidence_id=%s",
                (evidence_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise LookupError("NSE strategy evidence does not exist")
            identity = StrategyIdentity(
                Market(str(row[2])),
                str(row[3]),
                str(row[4]),
                str(row[5]),
                str(row[6]),
                int(cast(Any, row[7])),
            )
            return PromotionTarget(
                str(row[0]),
                str(row[1]),
                identity,
                ResearchState(str(row[8])),
                bool(row[9]),
            )
        finally:
            connection.close()

    def record_promotion(
        self,
        *,
        evidence_id: str,
        approval_id: str,
        reviewed_by: str,
        reason: str,
        promoted_at: datetime,
    ) -> str:
        promotion_id = stable_id(evidence_id, approval_id, reviewed_by, promoted_at.isoformat())
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO research.nse_strategy_promotions "
                "(promotion_id,evidence_id,approval_id,reviewed_by,review_reason,promoted_at) "
                "SELECT %s,%s,%s,%s,%s,%s FROM research.nse_strategy_evidence e "
                "JOIN research.validation_runs v ON v.validation_run_id=e.validation_run_id "
                "JOIN research.strategy_approvals a ON a.approval_id=%s "
                "WHERE e.evidence_id=%s AND e.research_state='RESEARCH' AND v.passed=true "
                "AND a.validation_run_id=e.validation_run_id "
                "AND a.strategy_key=e.strategy_key AND a.state='APPROVED' "
                "ON CONFLICT (promotion_id) DO NOTHING",
                (
                    promotion_id,
                    evidence_id,
                    approval_id,
                    reviewed_by,
                    reason,
                    promoted_at,
                    approval_id,
                    evidence_id,
                ),
            )
            cursor.execute(
                "SELECT evidence_id,approval_id,reviewed_by,review_reason,promoted_at "
                "FROM research.nse_strategy_promotions WHERE promotion_id=%s",
                (promotion_id,),
            )
            expected = (evidence_id, approval_id, reviewed_by, reason, promoted_at)
            if cursor.fetchone() != expected:
                raise PermissionError("promotion linkage is missing or immutable")
            connection.commit()
            return promotion_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def strategies(
        self,
        *,
        strategy_id: str | None = None,
        timeframe: str | None = None,
        lifecycle_state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[dict[str, object], ...]:
        where, params = self._filters(
            strategy_id=strategy_id,
            timeframe=timeframe,
            lifecycle_state=lifecycle_state,
        )
        query = "SELECT * FROM research.nse_strategy_validation_read" + where
        query += " ORDER BY evaluated_at DESC NULLS LAST,strategy_id,timeframe LIMIT %s OFFSET %s"
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(query, (*params, limit, offset))
            columns = (
                "strategy_key",
                "strategy_id",
                "strategy_version",
                "timeframe",
                "trade_horizon",
                "feature_set_version",
                "family",
                "parameters",
                "evidence_id",
                "campaign_id",
                "research_state",
                "validation_run_id",
                "evaluated_at",
                "passed",
                "metrics",
                "policy",
                "rejection_reasons",
                "approval_id",
                "approval_state",
                "approved_at",
                "expires_at",
                "lifecycle_state",
            )
            return tuple(dict(zip(columns, row, strict=True)) for row in cursor.fetchall())
        finally:
            connection.close()

    def backtests(
        self,
        *,
        strategy_id: str | None = None,
        timeframe: str | None = None,
        research_state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[dict[str, object], ...]:
        clauses: list[str] = []
        params: list[object] = []
        for column, value in (
            ("strategy_id", strategy_id),
            ("timeframe", timeframe),
            ("research_state", research_state),
        ):
            if value is not None:
                clauses.append(f"{column}=%s")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = "SELECT * FROM research.nse_backtest_read" + where
        query += " ORDER BY evaluated_at DESC,strategy_id,timeframe LIMIT %s OFFSET %s"
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(query, (*params, limit, offset))
            columns = (
                "evidence_id",
                "campaign_id",
                "strategy_key",
                "strategy_id",
                "strategy_version",
                "timeframe",
                "evaluated_at",
                "requested_start",
                "requested_end",
                "minimum_history_days",
                "source_provider",
                "research_state",
                "passed",
                "metrics",
                "policy",
                "rejection_reasons",
                "walk_forward_windows",
                "cost_model",
                "stressed_net_expectancy",
                "data_fingerprint",
            )
            return tuple(dict(zip(columns, row, strict=True)) for row in cursor.fetchall())
        finally:
            connection.close()

    @staticmethod
    def _filters(
        *,
        strategy_id: str | None,
        timeframe: str | None,
        lifecycle_state: str | None,
    ) -> tuple[str, tuple[object, ...]]:
        clauses: list[str] = []
        params: list[object] = []
        for column, value in (
            ("strategy_id", strategy_id),
            ("timeframe", timeframe),
            ("lifecycle_state", lifecycle_state),
        ):
            if value is not None:
                clauses.append(f"{column}=%s")
                params.append(value)
        return (" WHERE " + " AND ".join(clauses) if clauses else "", tuple(params))

    @staticmethod
    def _candle(row: tuple[object, ...]) -> SettledCandle:
        return SettledCandle(
            symbol=str(row[0]),
            timeframe=str(row[1]),
            open_time=cast(datetime, row[2]),
            open=float(cast(Any, row[3])),
            high=float(cast(Any, row[4])),
            low=float(cast(Any, row[5])),
            close=float(cast(Any, row[6])),
            volume=float(cast(Any, row[7])),
            provider=Provider(str(row[8])),
        )
