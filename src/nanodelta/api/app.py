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

from nanodelta.contracts import Market
from nanodelta.history.engine import BackfillEngine, HistoryJob
from nanodelta.operations import Actor, Command, OperationalStore, RuntimeController


@dataclass(frozen=True)
class ApiServices:
    operations: OperationalStore
    controller: RuntimeController
    history_engines: Mapping[Market, BackfillEngine]
    history_jobs: Mapping[tuple[Market, str, str], HistoryJob]
    api_keys: Mapping[str, Actor]


class Confirmation(BaseModel):
    confirmed: bool = False


class RepairBody(Confirmation):
    symbol: str = Field(min_length=1)
    timeframe: str = Field(min_length=1)
    gaps: list[datetime] = Field(default_factory=list, max_length=500)


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
    def overview() -> dict[str, Any]:
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

    @app.get("/api/{market}/health")
    def health(market: str) -> dict[str, Any]:
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
    ) -> Any:
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
        def read_collection(market: str) -> list[dict[str, Any]]:
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
