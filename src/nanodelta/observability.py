"""Bounded-cardinality logging and metrics for NanoDelta services."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.exposition import generate_latest

from nanodelta.contracts import Market
from nanodelta.operations import OperationalStore, WorkerState

CORRELATION_HEADER = "X-Correlation-ID"
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_safe_correlation_id = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def correlation_id() -> str | None:
    """Return the request correlation ID in the current execution context."""
    return _correlation_id.get()


def bind_correlation_id(value: str) -> Token[str | None]:
    return _correlation_id.set(value)


def reset_correlation_id(token: Token[str | None]) -> None:
    _correlation_id.reset(token)


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line without serialising secrets or arbitrary extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = correlation_id()
        if request_id is not None:
            payload["correlation_id"] = request_id
        for name in ("event", "method", "route", "status_code", "market", "duration_ms"):
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def configure_json_logging(level: str | None = None) -> None:
    """Configure process logging once for container-friendly stdout collection."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel((level or os.environ.get("LOG_LEVEL", "INFO")).upper())
    # httpx/httpcore log full request URLs (including query-string credentials, e.g.
    # Dhan's PIN+TOTP token endpoint) at INFO by default -- that's their own
    # unstructured logging, not NanoDelta's, and it leaks secrets into container logs.
    # Every provider client in this codebase goes through httpx; silence both loggers
    # rather than special-casing one provider.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


class ApiMetrics:
    """API metrics whose labels are deliberately restricted to small finite sets."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "nanodelta_http_requests_total",
            "HTTP requests received by the NanoDelta API.",
            ("method", "route", "status", "market"),
            registry=self.registry,
        )
        self.duration = Histogram(
            "nanodelta_http_request_duration_seconds",
            "NanoDelta API response latency.",
            ("method", "route", "market"),
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
            registry=self.registry,
        )
        self.in_progress = Gauge(
            "nanodelta_http_requests_in_progress",
            "NanoDelta API requests currently executing.",
            ("method",),
            registry=self.registry,
        )
        self.worker_state = Gauge(
            "nanodelta_market_worker_state",
            "One for the current market worker state and zero for other states.",
            ("market", "state"),
            registry=self.registry,
        )
        self.heartbeat_age = Gauge(
            "nanodelta_market_heartbeat_age_seconds",
            "Seconds since the latest market worker heartbeat, or -1 when none exists.",
            ("market",),
            registry=self.registry,
        )

    def observe_operations(self, operations: OperationalStore) -> None:
        now = datetime.now(UTC)
        for market in Market:
            current = operations.worker_state(market)
            for state in WorkerState:
                self.worker_state.labels(market=market.value, state=state.value).set(
                    int(current is state)
                )
            heartbeat = operations.heartbeats.get(market)
            age = -1.0 if heartbeat is None else max(0.0, (now - heartbeat).total_seconds())
            self.heartbeat_age.labels(market=market.value).set(age)


class RuntimeMetrics:
    """Metrics for the paper runtime with finite market/provider/result labels only."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.provider_events = Counter(
            "nanodelta_provider_events_total",
            "Normalized realtime provider events.",
            ("market", "provider"),
            registry=self.registry,
        )
        self.websocket_failovers = Counter(
            "nanodelta_websocket_failovers_total",
            "Realtime provider failovers.",
            ("market", "from_provider", "to_provider"),
            registry=self.registry,
        )
        self.websocket_sequence_gaps = Counter(
            "nanodelta_websocket_sequence_gaps_total",
            "Detected realtime sequence gaps.",
            ("market", "provider"),
            registry=self.registry,
        )
        self.cycle_duration = Histogram(
            "nanodelta_runtime_cycle_duration_seconds",
            "End-to-end runtime cycle duration.",
            ("market", "result"),
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
            registry=self.registry,
        )
        self.database_duration = Histogram(
            "nanodelta_database_operation_duration_seconds",
            "Database operation duration by bounded operation and result.",
            ("market", "operation", "result"),
            buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
            registry=self.registry,
        )
        self.decision_duration = Histogram(
            "nanodelta_decision_pipeline_duration_seconds",
            "Gold-to-paper-decision pipeline duration.",
            ("market", "result"),
            buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
            registry=self.registry,
        )

    def event(self, market: Market, provider: str) -> None:
        self.provider_events.labels(market=market.value, provider=provider).inc()

    def failover(self, market: Market, source: str, target: str) -> None:
        self.websocket_failovers.labels(
            market=market.value, from_provider=source, to_provider=target
        ).inc()

    def sequence_gap(self, market: Market, provider: str) -> None:
        self.websocket_sequence_gaps.labels(market=market.value, provider=provider).inc()

    def observe_cycle(self, market: Market, result: str, seconds: float) -> None:
        self.cycle_duration.labels(market=market.value, result=result).observe(seconds)

    def observe_database(self, market: Market, operation: str, result: str, seconds: float) -> None:
        self.database_duration.labels(
            market=market.value, operation=operation, result=result
        ).observe(seconds)

    def observe_decision(self, market: Market, result: str, seconds: float) -> None:
        self.decision_duration.labels(market=market.value, result=result).observe(seconds)


def _market_label(path: str) -> str:
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "api" and parts[1] in {market.value for market in Market}:
        return parts[1]
    return "global"


def _request_id(request: Request) -> str:
    candidate = request.headers.get(CORRELATION_HEADER)
    if candidate and _safe_correlation_id.fullmatch(candidate):
        return candidate
    return str(uuid.uuid4())


def install_observability(app: FastAPI, operations: OperationalStore | None = None) -> ApiMetrics:
    """Install correlation, request logs and metrics on one FastAPI application."""
    metrics = ApiMetrics()
    app.state.metrics = metrics
    request_logger = logging.getLogger("nanodelta.http")

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        if operations is not None:
            metrics.observe_operations(operations)
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.middleware("http")
    async def observe_request(request: Request, call_next: Any) -> Response:
        request_id = _request_id(request)
        token = bind_correlation_id(request_id)
        method = request.method
        market = _market_label(request.url.path)
        started = time.perf_counter()
        status_code = 500
        metrics.in_progress.labels(method=method).inc()
        try:
            response = cast(Response, await call_next(request))
            status_code = response.status_code
            response.headers[CORRELATION_HEADER] = request_id
            return response
        finally:
            elapsed = time.perf_counter() - started
            route_object = request.scope.get("route")
            route = getattr(route_object, "path", "unmatched")
            metrics.requests.labels(
                method=method,
                route=route,
                status=str(status_code),
                market=market,
            ).inc()
            metrics.duration.labels(method=method, route=route, market=market).observe(elapsed)
            metrics.in_progress.labels(method=method).dec()
            request_logger.info(
                "request completed",
                extra={
                    "event": "http_request_completed",
                    "method": method,
                    "route": route,
                    "status_code": status_code,
                    "market": market,
                    "duration_ms": round(elapsed * 1000, 3),
                },
            )
            reset_correlation_id(token)

    return metrics
