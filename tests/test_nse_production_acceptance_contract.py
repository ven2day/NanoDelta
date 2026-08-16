from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "scripts/acceptance/nse/run.py"
TEMPLATE = ROOT / "scripts/acceptance/nse/evidence-not-run.json"
DOC = ROOT / "docs/NSE_PRODUCTION_ACCEPTANCE.md"


def load_runner() -> object:
    spec = importlib.util.spec_from_file_location("nse_acceptance", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nse_acceptance_template_is_exhaustive_and_not_run() -> None:
    runner = load_runner()
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    assert template["schema_version"] == "1.0"
    assert template["status"] == "NOT_RUN"
    assert template["trading_mode"] == "PAPER"
    assert set(template["scenarios"]) == set(runner.SCENARIOS)  # type: ignore[attr-defined]
    assert all(
        item == {"measurements": {}, "status": "NOT_RUN"} for item in template["scenarios"].values()
    )
    assert "PASSED" not in TEMPLATE.read_text(encoding="utf-8")


def test_confirmation_requires_every_scenario_and_release_binding(tmp_path: Path) -> None:
    runner = load_runner()
    confirmation = tmp_path / "confirmation.json"
    confirmation.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "environment": "nse-paper-prod-1",
                "release_sha": "abc123",
                "approved_by": "operator",
                "approved_at": "2026-08-17T03:00:00+00:00",
                "change_ticket": "CHG-1",
                "scenarios": {},
            }
        ),
        encoding="utf-8",
    )
    try:
        runner.load_confirmation(  # type: ignore[attr-defined]
            confirmation, environment="nse-paper-prod-1", release_sha="abc123"
        )
    except ValueError as exc:
        assert "explicitly confirmed" in str(exc)
    else:
        raise AssertionError("an incomplete confirmation must fail closed")


def test_metric_parser_scopes_nse_provider_and_histogram_quantile() -> None:
    runner = load_runner()
    metrics = "\n".join(
        (
            "# TYPE nanodelta_provider_events_total counter",
            'nanodelta_provider_events_total{market="nse",provider="truedata"} 12',
            'nanodelta_provider_events_total{market="forex",provider="truedata"} 99',
        )
    )
    assert (
        runner.metric_sum(  # type: ignore[attr-defined]
            metrics,
            "nanodelta_provider_events_total",
            {"market": "nse", "provider": "truedata"},
        )
        == 12
    )
    assert (
        runner.histogram_quantile(  # type: ignore[attr-defined]
            {0.1: 50, 0.5: 95, 1.0: 99, float("inf"): 100}, 0.95
        )
        == 0.5
    )


def test_runner_reuses_destructive_shared_acceptance_and_keeps_database_reads_read_only() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    assert 'invoke_shared(args, "backup-restore"' in source
    assert '"alert-delivery"' in source
    assert "SET default_transaction_read_only = on" in source
    assert "--allow-runtime-restart" in source
    assert "--allow-provider-interruption" in source
    assert "--confirm-disposable-restore" in source
    assert 'execution_mode": "external' in source
    assert 'trading_mode": "PAPER' in source
    assert "live" not in source.lower().split("never writes orders", 1)[0]


def test_runbook_has_one_suite_command_and_explicit_scope_boundaries() -> None:
    documentation = DOC.read_text(encoding="utf-8")
    assert "python scripts/acceptance/nse/run.py suite" in documentation
    assert "Repository status: **NOT RUN**" in documentation
    assert "single-host" in documentation
    assert "live trading" in documentation.lower()
    assert "high availability" in documentation.lower()
    for scenario in (
        "Dhan history",
        "TrueData realtime soak",
        "TimescaleDB paper lifecycle",
        "Runtime restart",
        "Provider failover",
        "Backup and restore",
        "Decision latency",
        "Alertmanager receipt",
    ):
        assert scenario in documentation
