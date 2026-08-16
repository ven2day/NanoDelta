#!/usr/bin/env python3
"""VPS acceptance scenarios that fail closed and emit versioned JSON evidence."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from common import (
    command,
    command_from_file,
    command_to_file,
    fetch,
    now,
    require_external_confirmation,
    sha256,
    write_evidence,
)


def _metric(
    text: str, name: str, *, label: str | None = None, absent_value: float | None = None
) -> float:
    values = []
    for line in text.splitlines():
        if (line.startswith(name + "{") or line.startswith(name + " ")) and (
            label is None or label in line.split(" ", 1)[0]
        ):
            values.append(float(line.rsplit(" ", 1)[-1]))
    if not values:
        if absent_value is not None:
            return absent_value
        raise RuntimeError(f"required metric is absent: {name}")
    return sum(values)


def _scrape(url: str) -> dict[str, float]:
    text = fetch(url).decode()
    return {
        "events": _metric(text, "nanodelta_provider_events_total"),
        "failovers": _metric(text, "nanodelta_websocket_failovers_total"),
        "cycle_errors": _metric(
            text,
            "nanodelta_runtime_cycle_duration_seconds_count",
            label='result="error"',
            absent_value=0,
        ),
    }


def provider_soak(args: argparse.Namespace) -> dict[str, Any]:
    require_external_confirmation()
    baseline = _scrape(args.metrics_url)
    samples = 0
    deadline = time.monotonic() + args.duration
    latest = baseline
    while time.monotonic() < deadline:
        time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
        latest = _scrape(args.metrics_url)
        samples += 1
    event_delta = latest["events"] - baseline["events"]
    error_delta = latest["cycle_errors"] - baseline["cycle_errors"]
    if event_delta < args.minimum_events or error_delta > args.maximum_cycle_errors:
        raise RuntimeError("provider soak thresholds were not met")
    return {"samples": samples, "event_delta": event_delta, "cycle_error_delta": error_delta}


def load_latency(args: argparse.Namespace) -> dict[str, Any]:
    require_external_confirmation()
    timings: list[float] = []
    failures: list[str] = []

    def request() -> float:
        started = time.perf_counter()
        fetch(args.url, timeout=args.timeout)
        return time.perf_counter() - started

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(request) for _ in range(args.requests)]
        for future in as_completed(futures):
            try:
                timings.append(future.result())
            except Exception as exc:
                failures.append(type(exc).__name__)
    if not timings:
        raise RuntimeError("every load request failed")
    ordered = sorted(timings)
    p95 = ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]
    error_rate = len(failures) / args.requests
    if p95 > args.maximum_p95 or error_rate > args.maximum_error_rate:
        raise RuntimeError("load/latency thresholds were not met")
    return {
        "requests": args.requests,
        "concurrency": args.concurrency,
        "successes": len(timings),
        "failures": len(failures),
        "error_rate": error_rate,
        "p50_seconds": statistics.median(timings),
        "p95_seconds": p95,
    }


def provider_failover(args: argparse.Namespace) -> dict[str, Any]:
    require_external_confirmation()
    baseline = _scrape(args.metrics_url)
    print("Trigger the approved provider interruption now; waiting for metric evidence.")
    deadline = time.monotonic() + args.wait
    latest = baseline
    while time.monotonic() < deadline:
        time.sleep(args.interval)
        latest = _scrape(args.metrics_url)
        if latest["failovers"] > baseline["failovers"] and latest["events"] > baseline["events"]:
            break
    if latest["failovers"] <= baseline["failovers"]:
        raise RuntimeError("no provider failover counter increase was observed")
    if latest["events"] <= baseline["events"]:
        raise RuntimeError("no provider events were observed after failover")
    return {
        "failover_delta": latest["failovers"] - baseline["failovers"],
        "event_delta": latest["events"] - baseline["events"],
        "operator_interruption_required": True,
    }


def timescale_recovery(args: argparse.Namespace) -> dict[str, Any]:
    require_external_confirmation()
    if not args.allow_service_disruption:
        raise RuntimeError("--allow-service-disruption is required")
    fetch(args.ready_url)
    compose_files = [item for path in args.compose_file for item in ("-f", path)]
    compose = ["docker", "compose", *compose_files, "stop", "db"]
    command(compose)
    outage_observed = False
    started = time.perf_counter()
    try:
        try:
            fetch(args.ready_url, timeout=2)
        except Exception:
            outage_observed = True
        if not outage_observed:
            raise RuntimeError("API readiness did not fail after database stop")
    finally:
        command(["docker", "compose", *compose_files, "start", "db"])
    deadline = time.monotonic() + args.recovery_timeout
    while time.monotonic() < deadline:
        try:
            fetch(args.ready_url, timeout=2)
            return {"outage_observed": True, "rto_seconds": time.perf_counter() - started}
        except Exception:
            time.sleep(args.interval)
    raise RuntimeError("database/API readiness did not recover before timeout")


def backup_restore(args: argparse.Namespace) -> dict[str, Any]:
    require_external_confirmation()
    if not args.confirm_disposable_restore:
        raise RuntimeError("--confirm-disposable-restore is required")
    compose_files = [item for path in args.compose_file for item in ("-f", path)]
    prefix = ["docker", "compose", *compose_files, "exec", "-T", "db"]
    source_time = command(
        [
            *prefix,
            "psql",
            "-XAt",
            "-U",
            args.user,
            "-d",
            args.database,
            "-c",
            "SELECT clock_timestamp()",
        ]
    ).decode().strip()
    args.destination.mkdir(parents=True, exist_ok=True)
    artifact = args.destination / f"nanodelta-{now().strftime('%Y%m%dT%H%M%SZ')}.dump"
    temporary = artifact.with_suffix(".dump.tmp")
    backup_started = time.perf_counter()
    try:
        command_to_file(
            [
                *prefix,
                "pg_dump",
                "-U",
                args.user,
                "-d",
                args.database,
                "--format=custom",
                "--no-owner",
            ],
            temporary,
        )
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError("pg_dump returned an empty artifact")
        temporary.replace(artifact)
    finally:
        temporary.unlink(missing_ok=True)
    checksum = sha256(artifact)
    artifact.with_suffix(".dump.sha256").write_text(
        f"{checksum}  {artifact.name}\n", encoding="utf-8"
    )
    backup_seconds = time.perf_counter() - backup_started
    backup_finished = now()
    target = f"nanodelta_restore_{uuid.uuid4().hex[:12]}"
    restore_started = time.perf_counter()
    command([*prefix, "createdb", "-U", args.user, target])
    try:
        command_from_file(
            [*prefix, "pg_restore", "-U", args.user, "-d", target, "--no-owner", "--exit-on-error"],
            artifact,
        )
        migration_count = int(
            command(
                [
                    *prefix,
                    "psql",
                    "-XAt",
                    "-U",
                    args.user,
                    "-d",
                    target,
                    "-c",
                    "SELECT count(*) FROM control.schema_migrations",
                ]
            ).decode()
        )
        if migration_count < 1 or sha256(artifact) != checksum:
            raise RuntimeError("restored schema or checksum verification failed")
        return {
            "artifact": artifact.name,
            "bytes": artifact.stat().st_size,
            "sha256": checksum,
            "source_observed_at": source_time,
            "rpo_seconds": max(
                0.0,
                (backup_finished - datetime.fromisoformat(source_time)).total_seconds(),
            ),
            "backup_seconds": backup_seconds,
            "rto_seconds": time.perf_counter() - restore_started,
            "restored_migration_count": migration_count,
            "restore_target_disposable": True,
        }
    finally:
        command([*prefix, "dropdb", "-U", args.user, "--if-exists", target])


def alert_delivery(args: argparse.Namespace) -> dict[str, Any]:
    require_external_confirmation()
    evidence_id = f"nanodelta-evidence-{uuid.uuid4()}"
    payload = json.dumps(
        [
            {
                "labels": {
                    "alertname": "NanoDeltaAcceptanceDelivery",
                    "severity": "warning",
                    "evidence_id": evidence_id,
                }
            }
        ]
    ).encode()
    request = urllib.request.Request(
        args.alertmanager_url.rstrip("/") + "/api/v2/alerts",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
        if response.status not in {200, 202}:
            raise RuntimeError("Alertmanager rejected the acceptance alert")
    deadline = time.monotonic() + args.delivery_timeout
    while time.monotonic() < deadline:
        receipt = fetch(args.receipt_url, timeout=5).decode()
        if evidence_id in receipt:
            return {"evidence_id": evidence_id, "receiver_acknowledged": True}
        time.sleep(args.interval)
    raise RuntimeError("configured receiver did not acknowledge the evidence ID")


SCENARIOS = {
    "provider-soak": provider_soak,
    "load-latency": load_latency,
    "provider-failover": provider_failover,
    "timescale-recovery": timescale_recovery,
    "backup-restore": backup_restore,
    "alert-delivery": alert_delivery,
}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("scenario", choices=SCENARIOS)
    result.add_argument("--evidence", type=Path, required=True)
    result.add_argument("--metrics-url", default="http://127.0.0.1:9101/metrics")
    result.add_argument("--url", default="http://127.0.0.1:8000/health/ready")
    result.add_argument("--ready-url", default="http://127.0.0.1:8000/health/ready")
    result.add_argument("--alertmanager-url", default="http://127.0.0.1:9093")
    result.add_argument("--receipt-url", default="")
    result.add_argument("--duration", type=float, default=3600)
    result.add_argument("--interval", type=float, default=5)
    result.add_argument("--minimum-events", type=float, default=100)
    result.add_argument("--maximum-cycle-errors", type=float, default=0)
    result.add_argument("--requests", type=int, default=500)
    result.add_argument("--concurrency", type=int, default=20)
    result.add_argument("--timeout", type=float, default=5)
    result.add_argument("--maximum-p95", type=float, default=1)
    result.add_argument("--maximum-error-rate", type=float, default=0.01)
    result.add_argument("--wait", type=float, default=300)
    result.add_argument("--recovery-timeout", type=float, default=180)
    result.add_argument("--delivery-timeout", type=float, default=120)
    result.add_argument("--allow-service-disruption", action="store_true")
    result.add_argument("--confirm-disposable-restore", action="store_true")
    result.add_argument("--compose-file", action="append", default=[])
    result.add_argument("--destination", type=Path, default=Path("backups"))
    result.add_argument("--user", default=os.environ.get("POSTGRES_USER", "nanodelta"))
    result.add_argument("--database", default=os.environ.get("POSTGRES_DB", "nanodelta"))
    return result


def main() -> int:
    args = parser().parse_args()
    started = now()
    try:
        measurements = SCENARIOS[args.scenario](args)
        write_evidence(
            args.evidence,
            scenario=args.scenario,
            status="PASSED",
            started_at=started,
            measurements=measurements,
        )
        return 0
    except Exception as exc:
        write_evidence(
            args.evidence,
            scenario=args.scenario,
            status="FAILED",
            started_at=started,
            reason=str(exc),
        )
        print(f"{args.scenario} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
