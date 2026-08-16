"""FastAPI endpoints backed only by authoritative NanoDelta services."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from nanodelta.api.read_models import AuthoritativeReadStore, Page
from nanodelta.contracts import Market
from nanodelta.decisions import DecisionLedger
from nanodelta.finops import Attribution, FinOpsGuard, QwenFinOpsGateway
from nanodelta.history.engine import BackfillEngine, HistoryJob
from nanodelta.operations import Actor, Command, OperationalStore, RuntimeController


@dataclass(frozen=True)
class ApiServices:
    operations: OperationalStore
    controller: RuntimeController
    history_engines: Mapping[Market, BackfillEngine]
    history_jobs: Mapping[tuple[Market, str, str], HistoryJob]
    api_keys: Mapping[str, Actor]
    finops: FinOpsGuard | None = None
    qwen_gateway: QwenFinOpsGateway | None = None
    decision_ledger: DecisionLedger | None = None
    read_store: AuthoritativeReadStore | None = None


class Confirmation(BaseModel):
    confirmed: bool = False


class RepairBody(Confirmation):
    symbol: str = Field(min_length=1)
    timeframe: str = Field(min_length=1)
    gaps: list[datetime] = Field(default_factory=list, max_length=500)


class KillSwitchBody(BaseModel):
    active: bool
    reason: str = Field(min_length=1)


def create_app(services: ApiServices) -> FastAPI:
    app = FastAPI(title="NanoDelta API", version="0.1.0")
    key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def authenticate(key: str | None = Depends(key_header)) -> Actor:
        if key is None:
            raise HTTPException(status_code=401, detail="API key required")
        for configured, actor in services.api_keys.items():
            if secrets.compare_digest(key, configured):
                return actor
        raise HTTPException(status_code=401, detail="invalid API key")

    def viewer(actor: Actor = Depends(authenticate)) -> Actor:
        if actor.role not in {"viewer", "operator", "admin"}:
            raise HTTPException(status_code=403, detail="viewer, operator, or admin role required")
        return actor

    def market_value(value: str) -> Market:
        try:
            return Market(value)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="unknown market") from exc

    def serialize(value: object) -> Any:
        method = getattr(value, "to_dict", None)
        if callable(method):
            return method()
        if hasattr(value, "__dataclass_fields__"):
            return asdict(cast(Any, value))
        return value

    @app.get("/api/overview")
    def overview(actor: Actor = Depends(viewer)) -> dict[str, Any]:
        del actor
        return {
            "markets": {
                market.value: {
                    "worker_state": services.operations.workers[market],
                    "last_heartbeat": services.operations.heartbeats.get(market),
                    "provider_health": services.operations.provider_health[market],
                    "open_positions": len(services.operations.positions[market]),
                    "outcomes": len(services.operations.outcomes[market]),
                }
                for market in Market
            }
        }

    if services.finops is not None:

        @app.get("/api/finops")
        def finops_status(actor: Actor = Depends(viewer)) -> dict[str, Any]:
            del actor
            guard = services.finops
            assert guard is not None
            daily = guard.ledger.daily(datetime.now(UTC).date())
            return {
                "provider": guard.provider,
                "billing_mode": guard.billing_mode,
                "requests_today": daily.requests,
                "tokens_today": daily.tokens,
                "marginal_cost_usd_today": str(daily.marginal_cost_usd),
                "kill_switch": guard.ledger.kill_switch,
                "kill_reason": guard.ledger.kill_reason,
                "subscription_monthly_fee_usd": (
                    str(guard.subscription.monthly_fee_usd)
                    if guard.subscription is not None
                    else None
                ),
            }

        @app.get("/api/finops/alerts")
        def finops_alerts(actor: Actor = Depends(viewer)) -> list[Any]:
            del actor
            guard = services.finops
            assert guard is not None
            return [serialize(alert) for alert in guard.ledger.alerts.values()]

        @app.post("/api/finops/kill-switch")
        def finops_kill_switch(
            body: KillSwitchBody,
            actor: Actor = Depends(authenticate),
        ) -> dict[str, Any]:
            if actor.role != "admin":
                raise HTTPException(status_code=403, detail="admin role required")
            guard = services.finops
            assert guard is not None
            guard.set_kill_switch(
                body.active,
                reason=f"{actor.actor_id}: {body.reason}",
                now=datetime.now(UTC),
            )
            return {
                "active": guard.ledger.kill_switch,
                "reason": guard.ledger.kill_reason,
            }

    if services.qwen_gateway is not None:

        @app.post("/v1/chat/completions")
        async def qwen_chat_completions(
            body: dict[str, Any],
            actor: Actor = Depends(authenticate),
            estimated_input_tokens: int = Header(alias="X-Estimated-Input-Tokens", ge=0),
            market: str | None = Header(default=None, alias="X-NanoDelta-Market"),
            component: str = Header(alias="X-NanoDelta-Component", min_length=1),
            reason: str = Header(alias="X-NanoDelta-Reason", min_length=1),
        ) -> Any:
            del actor
            scoped_market = market_value(market) if market is not None else None
            gateway = services.qwen_gateway
            assert gateway is not None
            try:
                return await gateway.complete(
                    body,
                    attribution=Attribution(scoped_market, component, reason),
                    estimated_input_tokens=estimated_input_tokens,
                )
            except PermissionError as exc:
                raise HTTPException(status_code=429, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    if services.decision_ledger is not None:

        @app.get("/api/decision-cycles/{cycle_id}")
        def decision_cycle(cycle_id: str, actor: Actor = Depends(viewer)) -> list[Any]:
            del actor
            ledger = services.decision_ledger
            assert ledger is not None
            return [serialize(decision) for decision in ledger.for_cycle(cycle_id)]

    @app.get("/api/{market}/health")
    def health(market: str, actor: Actor = Depends(viewer)) -> dict[str, Any]:
        del actor
        scoped = market_value(market)
        return {
            "market": scoped,
            "worker_state": services.operations.workers[scoped],
            "last_heartbeat": services.operations.heartbeats.get(scoped),
            "providers": services.operations.provider_health[scoped],
        }

    @app.get("/api/{market}/history-status")
    def history_status(
        market: str,
        symbol: Annotated[str, Query(min_length=1)],
        timeframe: Annotated[str, Query(min_length=1)],
        actor: Actor = Depends(viewer),
    ) -> Any:
        del actor
        scoped = market_value(market)
        job = services.history_jobs.get((scoped, symbol, timeframe))
        engine = services.history_engines.get(scoped)
        if job is None or engine is None:
            raise HTTPException(status_code=404, detail="history job not configured")
        return serialize(engine.status(job, now=datetime.now(UTC)))

    collections = {
        "features": "features",
        "strategies": "strategies",
        "agent-runs": "agent_runs",
        "decisions": "decisions",
        "paper/positions": "positions",
        "paper/outcomes": "outcomes",
    }

    def collection_reader(attribute: str) -> Any:
        def read_collection(market: str, actor: Actor = Depends(viewer)) -> list[dict[str, Any]]:
            del actor
            scoped = market_value(market)
            collection = getattr(services.operations, attribute)
            return cast(list[dict[str, Any]], collection[scoped])

        return read_collection

    for route, attribute in collections.items():
        app.add_api_route(
            f"/api/{{market}}/{route}",
            collection_reader(attribute),
            methods=["GET"],
            name=f"read_{attribute}",
        )

    def authoritative_page(
        resource: str,
        market: Market | None,
        limit: int,
        offset: int,
        filters: dict[str, str],
    ) -> Page:
        if services.read_store is None:
            raise HTTPException(
                status_code=501,
                detail={"code": "AUTHORITATIVE_READ_MODEL_UNAVAILABLE", "resource": resource},
            )
        try:
            return services.read_store.page(
                resource, market=market, limit=limit, offset=offset, filters=filters
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def page_body(page: Page) -> dict[str, Any]:
        return {
            "items": [serialize(item) for item in page.items],
            "page": {"limit": page.limit, "offset": page.offset, "total": page.total},
            "freshness": {
                "freshest_at": page.freshest_at,
                "authoritative": True,
            },
        }

    def all_rows(
        resource: str, market: Market, filters: dict[str, str]
    ) -> tuple[Page, list[Mapping[str, Any]]]:
        first = authoritative_page(resource, market, 500, 0, filters)
        rows = list(first.items)
        offset = 500
        while offset < first.total:
            rows.extend(authoritative_page(resource, market, 500, offset, filters).items)
            offset += 500
        return first, rows

    @app.get("/api/{market}/candles")
    def candles(
        market: str,
        symbol: Annotated[str, Query(min_length=1)],
        timeframe: Annotated[str, Query(min_length=1)],
        limit: Annotated[int, Query(ge=1, le=5000)] = 500,
        offset: Annotated[int, Query(ge=0)] = 0,
        actor: Actor = Depends(viewer),
    ) -> dict[str, Any]:
        del actor
        return page_body(
            authoritative_page(
                "candles",
                market_value(market),
                limit,
                offset,
                {"symbol": symbol, "timeframe": timeframe},
            )
        )

    @app.get("/api/{market}/orders")
    def orders(
        market: str,
        symbol: str | None = None,
        action: str | None = None,
        state: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        actor: Actor = Depends(viewer),
    ) -> dict[str, Any]:
        del actor
        filters = {
            key: value
            for key, value in {"symbol": symbol, "action": action, "state": state}.items()
            if value is not None
        }
        return page_body(authoritative_page("orders", market_value(market), limit, offset, filters))

    @app.get("/api/{market}/trades")
    def trades(
        market: str,
        symbol: str | None = None,
        strategy_key: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        actor: Actor = Depends(viewer),
    ) -> dict[str, Any]:
        del actor
        filters = {
            key: value
            for key, value in {"symbol": symbol, "strategy_key": strategy_key}.items()
            if value is not None
        }
        return page_body(authoritative_page("trades", market_value(market), limit, offset, filters))

    @app.get("/api/{market}/positions")
    def positions(
        market: str,
        symbol: str | None = None,
        state: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        actor: Actor = Depends(viewer),
    ) -> dict[str, Any]:
        del actor
        filters = {
            key: value
            for key, value in {"symbol": symbol, "state": state}.items()
            if value is not None
        }
        return page_body(
            authoritative_page("positions", market_value(market), limit, offset, filters)
        )

    @app.get("/api/{market}/decision-events")
    def decision_events(
        market: str,
        symbol: str | None = None,
        timeframe: str | None = None,
        stage: str | None = None,
        status: str | None = None,
        reason_code: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        actor: Actor = Depends(viewer),
    ) -> dict[str, Any]:
        del actor
        values = locals()
        filters = {
            key: values[key]
            for key in ("symbol", "timeframe", "stage", "status", "reason_code")
            if values[key] is not None
        }
        return page_body(
            authoritative_page("decisions", market_value(market), limit, offset, filters)
        )

    @app.get("/api/{market}/risk/aggregate")
    def aggregate_risk(market: str, actor: Actor = Depends(viewer)) -> dict[str, Any]:
        del actor
        page, rows = all_rows("positions", market_value(market), {"state": "OPEN"})
        gross_entry_notional = sum(
            abs(float(row["signed_quantity"])) * float(row["average_entry_price"]) for row in rows
        )
        return {
            "market": market,
            "open_positions": page.total,
            "gross_entry_notional": gross_entry_notional,
            "realized_pnl": sum(float(row["realized_pnl"]) for row in rows),
            "total_fees": sum(float(row["total_fees"]) for row in rows),
            "unrealized_pnl": None,
            "unavailable_fields": [
                "unrealized_pnl",
                "mark_to_market_exposure",
                "remaining_daily_risk",
            ],
            "freshness": {"freshest_at": page.freshest_at, "authoritative": True},
        }

    @app.get("/api/{market}/performance")
    def performance(market: str, actor: Actor = Depends(viewer)) -> dict[str, Any]:
        del actor
        page, rows = all_rows("trades", market_value(market), {})
        net = [float(row["net_pnl"]) for row in rows]
        return {
            "market": market,
            "closed_trades": page.total,
            "gross_pnl": sum(float(row["gross_pnl"]) for row in rows),
            "net_pnl": sum(net),
            "total_fees": sum(float(row["total_fees"]) for row in rows),
            "wins": sum(value > 0 for value in net),
            "win_rate": (sum(value > 0 for value in net) / len(net)) if net else None,
            "unavailable_fields": ["sharpe", "sortino", "max_drawdown", "equity_curve"],
            "freshness": {"freshest_at": page.freshest_at, "authoritative": True},
        }

    def global_page_route(resource: str) -> Any:
        def read(
            market: str | None = None,
            limit: Annotated[int, Query(ge=1, le=500)] = 100,
            offset: Annotated[int, Query(ge=0)] = 0,
            actor: Actor = Depends(viewer),
        ) -> dict[str, Any]:
            del actor
            scoped = market_value(market) if market is not None else None
            return page_body(authoritative_page(resource, scoped, limit, offset, {}))

        return read

    for route, resource in {
        "alerts": "alerts",
        "reports": "reports",
        "settings": "settings",
        "audit": "audit",
        "strategy-lab/strategies": "strategies",
        "strategy-lab/validations": "validations",
    }.items():
        app.add_api_route(
            f"/api/{route}",
            global_page_route(resource),
            methods=["GET"],
            name=f"authoritative_{resource}",
        )

    @app.post("/api/{market}/runtime/{command}")
    def runtime_command(
        market: str,
        command: str,
        body: Confirmation,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        actor: Actor = Depends(authenticate),
    ) -> Any:
        scoped = market_value(market)
        try:
            parsed = Command(command)
            if parsed is Command.REPAIR:
                raise ValueError
            return serialize(
                services.controller.command(
                    scoped,
                    parsed,
                    actor,
                    idempotency_key=idempotency_key,
                    confirmed=body.confirmed,
                    requested_at=datetime.now(UTC),
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="unknown runtime command") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/{market}/history-repair")
    async def history_repair(
        market: str,
        body: RepairBody,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        actor: Actor = Depends(authenticate),
    ) -> dict[str, Any]:
        scoped = market_value(market)
        job = services.history_jobs.get((scoped, body.symbol, body.timeframe))
        engine = services.history_engines.get(scoped)
        if job is None or engine is None:
            raise HTTPException(status_code=404, detail="history job not configured")
        try:
            already_applied = services.operations.audit_record(idempotency_key) is not None
            audit = services.controller.command(
                scoped,
                Command.REPAIR,
                actor,
                idempotency_key=idempotency_key,
                confirmed=body.confirmed,
                requested_at=datetime.now(UTC),
                detail=f"{body.symbol}/{body.timeframe}",
            )
            runs = (
                ()
                if already_applied
                else await engine.repair(job, tuple(body.gaps), now=datetime.now(UTC))
            )
            return {"audit": serialize(audit), "runs": [serialize(run) for run in runs]}
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app
