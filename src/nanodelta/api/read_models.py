"""Authoritative UI read models backed by immutable NanoDelta records."""

# ruff: noqa: E501 -- SQL projections remain single literals for review and query auditing.

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row

from nanodelta.contracts import Market


@dataclass(frozen=True)
class Page:
    items: tuple[Mapping[str, Any], ...]
    total: int
    limit: int
    offset: int
    freshest_at: datetime | None


class AuthoritativeReadStore(Protocol):
    def page(
        self,
        resource: str,
        *,
        market: Market | None,
        limit: int,
        offset: int,
        filters: Mapping[str, str],
    ) -> Page: ...


class InMemoryAuthoritativeReadStore:
    """Deterministic adapter for tests; callers must explicitly seed every record."""

    def __init__(self) -> None:
        self.records: dict[str, list[dict[str, Any]]] = {}

    def seed(self, resource: str, records: Sequence[Mapping[str, Any]]) -> None:
        self.records[resource] = [dict(record) for record in records]

    def page(
        self,
        resource: str,
        *,
        market: Market | None,
        limit: int,
        offset: int,
        filters: Mapping[str, str],
    ) -> Page:
        rows = self.records.get(resource, [])
        selected = [
            row
            for row in rows
            if (market is None or row.get("market") == market.value)
            and all(str(row.get(key, "")) == value for key, value in filters.items())
        ]
        timestamps = [
            value
            for row in selected
            for key, value in row.items()
            if key in {"occurred_at", "updated_at", "open_time", "recorded_at", "created_at"}
            and isinstance(value, datetime)
        ]
        return Page(
            tuple(selected[offset : offset + limit]),
            len(selected),
            limit,
            offset,
            max(timestamps, default=None),
        )


@dataclass(frozen=True)
class _Query:
    select: str
    count: str
    market_column: str | None
    filters: Mapping[str, str]
    order_by: str
    freshness: str


_QUERIES: dict[str, _Query] = {
    "candles": _Query(
        "SELECT symbol,timeframe,open_time,provider,open,high,low,close,volume,is_settled FROM {schema}.candles",
        "SELECT count(*) AS count FROM {schema}.candles",
        None,
        {"symbol": "symbol", "timeframe": "timeframe"},
        "open_time DESC",
        "open_time",
    ),
    "features": _Query(
        "SELECT record_id,candle_record_id,symbol,timeframe,event_time,feature_version,features,created_at FROM {schema}.feature_snapshots",
        "SELECT count(*) AS count FROM {schema}.feature_snapshots",
        None,
        {"symbol": "symbol", "timeframe": "timeframe"},
        "event_time DESC",
        "event_time",
    ),
    "history": _Query(
        "SELECT run_id,market,symbol,timeframe,state,started_at,finished_at,provider,rows_received,bronze_created,silver_created,error_message FROM control.history_runs",
        "SELECT count(*) AS count FROM control.history_runs",
        "market",
        {"symbol": "symbol", "timeframe": "timeframe", "state": "state"},
        "started_at DESC",
        "started_at",
    ),
    "orders": _Query(
        "SELECT o.order_id,o.decision_id,o.market,o.account_id,o.symbol,o.action,o.quantity,o.state,o.execution_mode,o.submitted_at,f.fill_id,f.price AS fill_price,f.fee,f.filled_at,d.candidate_id,d.approval_id,d.reference_price,x.strategy_key,x.stop_price,x.target_price,x.state AS exit_plan_state FROM paper.orders o LEFT JOIN paper.fills f ON f.order_id=o.order_id LEFT JOIN paper.decisions d ON d.decision_id=o.decision_id LEFT JOIN paper.order_positions op ON op.order_id=o.order_id LEFT JOIN paper.exit_plans x ON x.position_id=op.position_id",
        "SELECT count(*) AS count FROM paper.orders o",
        "o.market",
        {"symbol": "o.symbol", "action": "o.action", "state": "o.state"},
        "o.submitted_at DESC",
        "o.submitted_at",
    ),
    "trades": _Query(
        "SELECT outcome_id,position_id,market,account_id,symbol,strategy_key,opened_at,closed_at,gross_pnl,total_fees,net_pnl,return_on_allocated_capital,recorded_at FROM paper.outcomes",
        "SELECT count(*) AS count FROM paper.outcomes",
        "market",
        {"symbol": "symbol", "strategy_key": "strategy_key"},
        "recorded_at DESC",
        "recorded_at",
    ),
    "positions": _Query(
        "SELECT position_id,market,account_id,symbol,signed_quantity,average_entry_price,realized_pnl,total_fees,opened_at,updated_at,closed_at,state,strategy_keys FROM paper.positions",
        "SELECT count(*) AS count FROM paper.positions",
        "market",
        {"symbol": "symbol", "state": "state"},
        "updated_at DESC",
        "updated_at",
    ),
    "decisions": _Query(
        "SELECT decision_id,cycle_id,market,symbol,timeframe,stage,status,reason_code,occurred_at,candidate_id,strategy_key,detail,metrics FROM control.decision_events",
        "SELECT count(*) AS count FROM control.decision_events",
        "market",
        {
            "symbol": "symbol",
            "timeframe": "timeframe",
            "stage": "stage",
            "status": "status",
            "reason_code": "reason_code",
            "cycle_id": "cycle_id",
        },
        "occurred_at DESC",
        "occurred_at",
    ),
    "alerts": _Query(
        "SELECT alert_id,market,severity,component,reason_code,detail,state,occurred_at,acknowledged_at,resolved_at FROM control.alert_events",
        "SELECT count(*) AS count FROM control.alert_events",
        "market",
        {"severity": "severity", "state": "state", "component": "component"},
        "occurred_at DESC",
        "occurred_at",
    ),
    "audit": _Query(
        "SELECT audit_id,idempotency_key,market,command,actor_id,previous_state,resulting_state,requested_at,detail FROM control.operational_audit",
        "SELECT count(*) AS count FROM control.operational_audit",
        "market",
        {"actor_id": "actor_id", "command": "command"},
        "requested_at DESC",
        "requested_at",
    ),
    "settings": _Query(
        "SELECT setting_key,market,value,updated_at,updated_by FROM control.system_settings",
        "SELECT count(*) AS count FROM control.system_settings",
        "market",
        {"setting_key": "setting_key"},
        "updated_at DESC",
        "updated_at",
    ),
    "reports": _Query(
        "SELECT report_id,market,report_type,state,parameters,artifact_uri,started_at,completed_at,requested_by FROM control.report_runs",
        "SELECT count(*) AS count FROM control.report_runs",
        "market",
        {"report_type": "report_type", "state": "state"},
        "started_at DESC",
        "started_at",
    ),
    "strategies": _Query(
        "SELECT d.strategy_key,d.market,d.strategy_id,d.strategy_version,d.timeframe,d.trade_horizon,d.feature_set_version,d.family,d.parameters,d.implementation_ref,d.created_at,a.approval_id,a.state AS approval_state,a.expires_at FROM research.strategy_definitions d LEFT JOIN LATERAL (SELECT approval_id,state,expires_at FROM research.strategy_approvals WHERE strategy_key=d.strategy_key ORDER BY approved_at DESC LIMIT 1) a ON true",
        "SELECT count(*) AS count FROM research.strategy_definitions d",
        "d.market",
        {"strategy_id": "d.strategy_id", "timeframe": "d.timeframe", "family": "d.family"},
        "d.created_at DESC",
        "d.created_at",
    ),
    "validations": _Query(
        "SELECT v.validation_run_id,v.strategy_key,d.market,v.evaluated_at,v.passed,v.metrics,v.policy,v.rejection_reasons FROM research.validation_runs v JOIN research.strategy_definitions d ON d.strategy_key=v.strategy_key",
        "SELECT count(*) AS count FROM research.validation_runs v JOIN research.strategy_definitions d ON d.strategy_key=v.strategy_key",
        "d.market",
        {"strategy_key": "v.strategy_key", "passed": "v.passed"},
        "v.evaluated_at DESC",
        "v.evaluated_at",
    ),
}


class PostgresAuthoritativeReadStore:
    """Fixed-query PostgreSQL adapter; resource and filter SQL are never user supplied."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def page(
        self,
        resource: str,
        *,
        market: Market | None,
        limit: int,
        offset: int,
        filters: Mapping[str, str],
    ) -> Page:
        query = _QUERIES.get(resource)
        if query is None:
            raise ValueError("unsupported authoritative resource")
        if resource in {"candles", "features"} and market is None:
            raise ValueError(f"{resource} require a market")
        schema = ""
        if market is not None:
            schema = (
                f"{market.value}_gold" if resource == "features" else f"{market.value}_silver"
            )
        select = query.select.format(schema=schema)
        count = query.count.format(schema=schema)
        clauses: list[str] = []
        parameters: list[object] = []
        if query.market_column is not None and market is not None:
            clauses.append(f"{query.market_column} = %s")
            parameters.append(market.value)
        for name, value in filters.items():
            column = query.filters.get(name)
            if column is None:
                raise ValueError(f"unsupported filter: {name}")
            clauses.append(f"{column} = %s")
            parameters.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(count + where, parameters)
                count_row = cursor.fetchone()
                if count_row is None:
                    raise RuntimeError("authoritative count query returned no row")
                total = int(count_row["count"])
                cursor.execute(
                    f"{select}{where} ORDER BY {query.order_by} LIMIT %s OFFSET %s",
                    [*parameters, limit, offset],
                )
                items = tuple(cursor.fetchall())
        timestamps = [
            timestamp
            for row in items
            if isinstance((timestamp := row.get(query.freshness)), datetime)
        ]
        freshest = max(timestamps, default=None)
        return Page(items, total, limit, offset, freshest)
