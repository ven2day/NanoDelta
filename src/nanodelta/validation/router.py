"""Composable authoritative NSE validation routes.

The main application must supply its existing authenticated operator dependency
when including this router.  This module intentionally does not assemble the app.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, Protocol

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder


class NseValidationReader(Protocol):
    def strategies(
        self,
        *,
        strategy_id: str | None = None,
        timeframe: str | None = None,
        lifecycle_state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[dict[str, object], ...]: ...

    def backtests(
        self,
        *,
        strategy_id: str | None = None,
        timeframe: str | None = None,
        research_state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[dict[str, object], ...]: ...


def build_nse_validation_router(
    reader: NseValidationReader,
    *,
    operator_guard: Callable[[], object],
) -> APIRouter:
    router = APIRouter(
        prefix="/api/nse/strategy-validation",
        tags=["nse-strategy-validation"],
        dependencies=[Depends(operator_guard)],
    )

    @router.get("/strategies")
    def strategies(
        strategy_id: str | None = None,
        timeframe: Literal["5m", "15m", "30m", "1h"] | None = None,
        lifecycle_state: Literal["RESEARCH", "FAILED", "PAPER_APPROVED"] | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> object:
        rows = reader.strategies(
            strategy_id=strategy_id,
            timeframe=timeframe,
            lifecycle_state=lifecycle_state,
            limit=limit,
            offset=offset,
        )
        return jsonable_encoder({"items": rows, "limit": limit, "offset": offset})

    @router.get("/backtests")
    def backtests(
        strategy_id: str | None = None,
        timeframe: Literal["5m", "15m", "30m", "1h"] | None = None,
        research_state: Literal["RESEARCH", "FAILED"] | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> object:
        rows = reader.backtests(
            strategy_id=strategy_id,
            timeframe=timeframe,
            research_state=research_state,
            limit=limit,
            offset=offset,
        )
        return jsonable_encoder({"items": rows, "limit": limit, "offset": offset})

    return router
