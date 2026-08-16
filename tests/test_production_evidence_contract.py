from __future__ import annotations

import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import yaml

ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "scripts/acceptance/run.py"


class HealthyHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ready"}')

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def test_external_scenario_fails_closed_and_writes_failed_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    result = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "provider-soak",
            "--duration",
            "0.01",
            "--evidence",
            str(evidence),
        ],
        cwd=ROOT,
        env={},
        check=False,
    )
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert result.returncode == 1
    assert payload["status"] == "FAILED"
    assert "EXTERNAL_CONFIRMED" in payload["reason"]
    assert payload["measurements"] == {}


def test_load_latency_runner_produces_measured_evidence(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), HealthyHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    evidence = tmp_path / "load.json"
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "load-latency",
                "--url",
                f"http://127.0.0.1:{server.server_port}/health/ready",
                "--requests",
                "10",
                "--concurrency",
                "2",
                "--maximum-p95",
                "5",
                "--evidence",
                str(evidence),
            ],
            cwd=ROOT,
            env={"NANODELTA_ACCEPTANCE_EXTERNAL_CONFIRMED": "true"},
            check=False,
        )
    finally:
        server.shutdown()
        thread.join()
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert result.returncode == 0
    assert payload["status"] == "PASSED"
    assert payload["measurements"]["successes"] == 10
    assert payload["measurements"]["failures"] == 0


def test_external_timescale_manifest_has_no_fake_database_or_failover() -> None:
    manifest_path = ROOT / "deploy/ha/docker-compose.external-timescale.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["services"]) == {"migrate", "api", "runtime", "web"}
    assert "db" not in manifest["services"]
    assert manifest["services"]["runtime"]["entrypoint"] == ["nanodelta-entrypoint"]
    assert manifest["services"]["runtime"]["command"] == ["nanodelta-runtime"]
    rendered = manifest_path.read_text(encoding="utf-8").lower()
    assert "promote" not in rendered
    assert all("replica" not in name for name in manifest["services"])


def test_restore_contract_is_disposable_and_repository_status_is_not_run() -> None:
    runner = RUNNER.read_text(encoding="utf-8")
    assert "--confirm-disposable-restore" in runner
    assert 'target = f"nanodelta_restore_' in runner
    assert '"dropdb"' in runner
    assert '"pg_dump"' in runner and "args.database" in runner
    template = json.loads(
        (ROOT / "scripts/acceptance/evidence-not-run.json").read_text(encoding="utf-8")
    )
    assert template["status"] == "NOT_RUN"
    assert template["measurements"] == {}
