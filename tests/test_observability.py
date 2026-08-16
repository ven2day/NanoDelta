from __future__ import annotations

import json
import logging
from io import StringIO
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nanodelta.api import runtime
from nanodelta.observability import JsonFormatter, bind_correlation_id, reset_correlation_id
from nanodelta.operations import OperationalStore


def _app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    secret = tmp_path / "admin_api_key"
    secret.write_text("test-admin-key", encoding="utf-8")
    monkeypatch.setenv("NANODELTA_ADMIN_API_KEY_FILE", str(secret))
    monkeypatch.setattr(runtime, "PostgresOperationalStore", lambda _connect: OperationalStore())
    return TestClient(runtime.build_app())


def test_request_correlation_id_is_returned_and_metrics_use_bounded_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _app(monkeypatch, tmp_path)
    response = client.get(
        "/api/nse/health",
        headers={"X-Correlation-ID": "cycle-123", "X-API-Key": "test-admin-key"},
    )
    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == "cycle-123"

    metrics = client.get("/metrics").text
    assert 'market="nse"' in metrics
    assert 'route="/api/{market}/health"' in metrics
    assert 'status="200"' in metrics
    assert 'nanodelta_market_worker_state{market="nse",state="STOPPED"} 1.0' in metrics
    assert 'nanodelta_market_worker_state{market="forex",state="STOPPED"} 1.0' in metrics
    assert 'nanodelta_market_worker_state{market="crypto",state="STOPPED"} 1.0' in metrics
    assert "symbol=" not in metrics
    assert "candidate=" not in metrics


def test_unsafe_correlation_id_is_replaced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    response = _app(monkeypatch, tmp_path).get(
        "/health/live", headers={"X-Correlation-ID": "unsafe value with spaces"}
    )
    generated = response.headers["X-Correlation-ID"]
    assert generated != "unsafe value with spaces"
    assert len(generated) == 36


def test_json_formatter_emits_selected_operational_fields_only() -> None:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("nanodelta.test.observability")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    token = bind_correlation_id("request-42")
    try:
        logger.info(
            "request completed",
            extra={"event": "http_request_completed", "market": "forex", "secret": "do-not-log"},
        )
    finally:
        reset_correlation_id(token)

    payload = json.loads(stream.getvalue())
    assert payload["correlation_id"] == "request-42"
    assert payload["market"] == "forex"
    assert "do-not-log" not in stream.getvalue()


def test_observability_configuration_is_parseable_and_has_all_services() -> None:
    root = Path(__file__).parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    assert set(compose["services"]) >= {"prometheus", "alertmanager", "grafana"}
    assert compose["services"]["prometheus"]["profiles"] == ["observability"]
    assert compose["services"]["grafana"]["profiles"] == ["observability"]

    prometheus = yaml.safe_load(
        (root / "deploy/observability/prometheus/prometheus.yml").read_text(encoding="utf-8")
    )
    assert prometheus["scrape_configs"][0]["static_configs"][0]["targets"] == ["api:8000"]
    alerts = yaml.safe_load(
        (root / "deploy/observability/prometheus/alerts.yml").read_text(encoding="utf-8")
    )
    names = {rule["alert"] for group in alerts["groups"] for rule in group["rules"]}
    assert names == {
        "NanoDeltaApiUnavailable",
        "NanoDeltaHighServerErrorRate",
        "NanoDeltaHighApiLatency",
    }
    json.loads(
        (root / "deploy/observability/grafana/dashboards/nanodelta-api.json").read_text(
            encoding="utf-8"
        )
    )
