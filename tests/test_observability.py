from __future__ import annotations

import json
import logging
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from prometheus_client.exposition import generate_latest

from nanodelta.api import runtime
from nanodelta.contracts import Market
from nanodelta.observability import (
    JsonFormatter,
    RuntimeMetrics,
    bind_correlation_id,
    configure_json_logging,
    reset_correlation_id,
)
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
    runtime_scrape = next(
        item for item in prometheus["scrape_configs"] if item["job_name"] == "nanodelta-runtime"
    )
    assert runtime_scrape["static_configs"][0]["targets"] == ["runtime:9101"]
    alerts = yaml.safe_load(
        (root / "deploy/observability/prometheus/alerts.yml").read_text(encoding="utf-8")
    )
    names = {rule["alert"] for group in alerts["groups"] for rule in group["rules"]}
    assert names == {
        "NanoDeltaApiUnavailable",
        "NanoDeltaHighServerErrorRate",
        "NanoDeltaHighApiLatency",
        "NanoDeltaRuntimeUnavailable",
        "NanoDeltaRuntimeCycleFailures",
        "NanoDeltaDecisionLatencyHigh",
        "NanoDeltaProviderSequenceGaps",
    }
    json.loads(
        (root / "deploy/observability/grafana/dashboards/nanodelta-api.json").read_text(
            encoding="utf-8"
        )
    )


def test_runtime_metrics_cover_pipeline_without_unbounded_labels() -> None:
    metrics = RuntimeMetrics()
    metrics.event(Market.CRYPTO, "okx")
    metrics.failover(Market.CRYPTO, "okx", "poloniex")
    metrics.sequence_gap(Market.CRYPTO, "okx")
    metrics.observe_cycle(Market.CRYPTO, "success", 0.2)
    metrics.observe_database(Market.CRYPTO, "latest_marks", "success", 0.01)
    metrics.observe_decision(Market.CRYPTO, "success", 0.03)
    rendered = generate_latest(metrics.registry).decode()

    for metric in (
        "nanodelta_provider_events_total",
        "nanodelta_websocket_failovers_total",
        "nanodelta_websocket_sequence_gaps_total",
        "nanodelta_runtime_cycle_duration_seconds",
        "nanodelta_database_operation_duration_seconds",
        "nanodelta_decision_pipeline_duration_seconds",
    ):
        assert metric in rendered
    assert 'market="crypto"' in rendered
    assert "symbol=" not in rendered
    assert "account=" not in rendered
    assert "candidate=" not in rendered


def test_acceptance_runner_is_honest_about_synthetic_and_external_runs(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    synthetic = tmp_path / "synthetic.json"
    subprocess.run(
        [
            sys.executable,
            str(root / "scripts/run-acceptance.py"),
            "load-latency",
            "--requests",
            "8",
            "--concurrency",
            "2",
            "--output",
            str(synthetic),
        ],
        check=True,
    )
    synthetic_payload = json.loads(synthetic.read_text())
    assert synthetic_payload["execution_mode"] == "synthetic"
    assert synthetic_payload["status"] == "PASSED"

    external = tmp_path / "external.json"
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/run-acceptance.py"),
            "provider-soak",
            "--require-external",
            "--output",
            str(external),
        ],
        check=False,
    )
    external_payload = json.loads(external.read_text())
    assert result.returncode == 1
    assert external_payload["status"] == "SKIPPED"
    assert external_payload["measurements"] == {}


def test_configure_json_logging_silences_httpx_request_url_logging() -> None:
    """httpx logs full request URLs (including query-string credentials -- e.g. Dhan's
    PIN+TOTP token endpoint, which takes pin/totp as query params) at INFO by default.
    Caught by actually running the deployed runtime container against real Dhan
    credentials: the PIN and a TOTP code landed in plaintext container logs. Every
    provider client in this codebase goes through httpx, so this isn't Dhan-specific."""
    try:
        configure_json_logging("INFO")
        assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
        assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
        assert logging.getLogger().getEffectiveLevel() == logging.INFO
    finally:
        logging.getLogger().handlers = []
        logging.getLogger("httpx").setLevel(logging.NOTSET)
        logging.getLogger("httpcore").setLevel(logging.NOTSET)
