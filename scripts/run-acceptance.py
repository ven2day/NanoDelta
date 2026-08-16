#!/usr/bin/env python3
"""Produce honest, machine-readable acceptance evidence.

Synthetic mode is CI-safe. External mode observes an already deployed paper runtime; it never
injects credentials or claims that an operator-triggered failover/recovery occurred.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

VERSION = "1.0"
EXTERNAL = {"provider-soak", "provider-failover", "db-recovery", "backup-restore"}


def _fetch(url: str, timeout: float) -> float:
    started = time.perf_counter()
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError(f"unexpected HTTP status {response.status}")
        response.read()
    return time.perf_counter() - started


def _synthetic(requests: int, concurrency: int) -> dict[str, Any]:
    def work(index: int) -> float:
        started = time.perf_counter()
        sum((index + offset) % 97 for offset in range(2_000))
        return time.perf_counter() - started

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        samples = list(executor.map(work, range(requests)))
    ordered = sorted(samples)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    return {
        "requests": requests,
        "concurrency": concurrency,
        "p50_ms": round(statistics.median(samples) * 1000, 3),
        "p95_ms": round(p95 * 1000, 3),
        "errors": 0,
    }


def _external_probe(url: str, samples: int, interval: float, timeout: float) -> dict[str, Any]:
    timings: list[float] = []
    for index in range(samples):
        timings.append(_fetch(url, timeout))
        if index + 1 < samples:
            time.sleep(interval)
    return {
        "url": url,
        "samples": samples,
        "p95_ms": round(sorted(timings)[max(0, int(len(timings) * 0.95) - 1)] * 1000, 3),
        "errors": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=["load-latency", *sorted(EXTERNAL)])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--require-external", action="store_true")
    args = parser.parse_args()
    started = datetime.now(UTC)
    status, measurements, reason = "PASSED", {}, None
    try:
        if args.scenario == "load-latency":
            if args.requests <= 0 or args.concurrency <= 0:
                raise ValueError("requests and concurrency must be positive")
            measurements = _synthetic(args.requests, args.concurrency)
        else:
            url = os.environ.get("NANODELTA_ACCEPTANCE_PROBE_URL", "").strip()
            confirmation = os.environ.get("NANODELTA_ACCEPTANCE_EXTERNAL_CONFIRMED") == "true"
            if not url or not confirmation:
                status = "SKIPPED"
                reason = (
                    "external run requires NANODELTA_ACCEPTANCE_PROBE_URL and "
                    "NANODELTA_ACCEPTANCE_EXTERNAL_CONFIRMED=true"
                )
            else:
                measurements = _external_probe(url, args.samples, args.interval, args.timeout)
                measurements["operator_action_required"] = args.scenario in {
                    "provider-failover",
                    "db-recovery",
                    "backup-restore",
                }
    except Exception as exc:
        status, reason = "FAILED", str(exc)
    evidence = {
        "schema_version": VERSION,
        "scenario": args.scenario,
        "execution_mode": "synthetic" if args.scenario == "load-latency" else "external",
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "git_sha": os.environ.get("GITHUB_SHA", "unknown"),
        "status": status,
        "reason": reason,
        "measurements": measurements,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if status == "FAILED" or (status == "SKIPPED" and args.require_external):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
