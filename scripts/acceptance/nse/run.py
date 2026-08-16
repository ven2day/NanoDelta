#!/usr/bin/env python3
"""Run the fail-closed NSE paper-production acceptance suite.

The suite reads provider credentials only through already-running services and a
database URL/API-key file. It never writes orders or enables live execution.
Disruptive actions require both an approved operator manifest and explicit CLI
flags. A suite can pass only when every external scenario produces measurements.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

SCHEMA_VERSION = "1.0"
REQUIRED_TIMEFRAMES = ("5m", "15m", "30m", "1h")
DEFAULT_MINIMUM_CANDLES = {"5m": 35000, "15m": 11500, "30m": 6000, "1h": 3200}
SCENARIOS = (
    "dhan_history_readiness",
    "truedata_realtime_soak",
    "timescaledb_paper_lifecycle",
    "runtime_restart_recovery",
    "provider_failover",
    "backup_restore",
    "decision_latency",
    "alertmanager_receipt",
)
SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})?\s+"
    r"(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|[-+]?Inf|NaN)$"
)
LABEL_RE = re.compile(r'(\w+)="((?:\\.|[^"\\])*)"')
SECRET_RE = re.compile(r"(?i)(postgres(?:ql)?://)([^@\s]+)@")


class ScenarioFailureError(RuntimeError):
    def __init__(self, reason: str, measurements: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.measurements = dict(measurements or {})


def now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def git_sha(root: Path) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def safe_reason(exc: BaseException) -> str:
    value = SECRET_RE.sub(r"\1***@", str(exc)).replace("\n", " ")
    return f"{type(exc).__name__}: {value[:500]}"


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    def encode(value: object) -> str:
        if isinstance(value, datetime):
            return iso(value) or ""
        if isinstance(value, Path):
            return str(value)
        raise TypeError(f"cannot encode evidence value: {type(value).__name__}")

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=encode, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def require_external_path(path: Path, root: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == root or root in resolved.parents:
        raise ValueError(f"{label} must be outside the Git checkout")
    return resolved


def read_nonempty(path: Path, label: str) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"{label} is empty")
    return value


def load_confirmation(path: Path, *, environment: str, release_sha: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("operator confirmation schema_version must be 1.0")
    if payload.get("environment") != environment:
        raise ValueError("operator confirmation environment does not match")
    if payload.get("release_sha") != release_sha:
        raise ValueError("operator confirmation release_sha does not match checkout")
    for field in ("approved_by", "approved_at", "change_ticket"):
        if not str(payload.get(field, "")).strip():
            raise ValueError(f"operator confirmation requires {field}")
    approved_at = datetime.fromisoformat(str(payload["approved_at"]))
    if approved_at.tzinfo is None:
        raise ValueError("operator confirmation approved_at must include a timezone")
    confirmations = payload.get("scenarios")
    if not isinstance(confirmations, dict):
        raise ValueError("operator confirmation requires a scenarios object")
    for scenario in SCENARIOS:
        item = confirmations.get(scenario)
        if not isinstance(item, dict) or item.get("confirmed") is not True:
            raise ValueError(f"scenario is not explicitly confirmed: {scenario}")
        if not str(item.get("reference", "")).strip():
            raise ValueError(f"scenario confirmation requires a reference: {scenario}")
    return dict(payload)


def fetch(url: str, *, headers: Mapping[str, str] | None = None, timeout: float = 10) -> bytes:
    request = urllib.request.Request(url, headers=dict(headers or {}))  # noqa: S310
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError(f"unexpected HTTP status {response.status} from {url}")
        return bytes(response.read())


def api_json(args: argparse.Namespace, path: str) -> dict[str, Any]:
    key = read_nonempty(args.api_key_file, "API key file")
    url = args.api_url.rstrip("/") + path
    payload = json.loads(fetch(url, headers={"X-API-Key": key}, timeout=args.http_timeout))
    if not isinstance(payload, dict):
        raise RuntimeError(f"authoritative API did not return an object: {path}")
    return dict(payload)


def metric_samples(text: str, name: str) -> list[tuple[dict[str, str], float]]:
    samples: list[tuple[dict[str, str], float]] = []
    for line in text.splitlines():
        match = SAMPLE_RE.match(line)
        if match is None or match.group("name") != name:
            continue
        labels = {
            key: json.loads(f'"{raw}"')
            for key, raw in LABEL_RE.findall(match.group("labels") or "")
        }
        samples.append((labels, float(match.group("value"))))
    return samples


def metric_sum(
    text: str,
    name: str,
    labels: Mapping[str, str],
    *,
    absent_value: float | None = None,
) -> float:
    values = [
        value
        for actual, value in metric_samples(text, name)
        if all(actual.get(key) == expected for key, expected in labels.items())
    ]
    if not values:
        if absent_value is not None:
            return absent_value
        raise RuntimeError(f"required metric is absent: {name} {dict(labels)}")
    return sum(values)


def scrape_metrics(args: argparse.Namespace) -> str:
    return fetch(args.metrics_url, timeout=args.http_timeout).decode("utf-8")


def runtime_metrics(args: argparse.Namespace) -> dict[str, float]:
    text = scrape_metrics(args)
    return {
        "truedata_events": metric_sum(
            text,
            "nanodelta_provider_events_total",
            {"market": "nse", "provider": "truedata"},
            absent_value=0,
        ),
        "dhan_events": metric_sum(
            text,
            "nanodelta_provider_events_total",
            {"market": "nse", "provider": "dhan"},
            absent_value=0,
        ),
        "failovers": metric_sum(
            text,
            "nanodelta_websocket_failovers_total",
            {"market": "nse", "from_provider": "truedata", "to_provider": "dhan"},
            absent_value=0,
        ),
        "gaps": metric_sum(
            text,
            "nanodelta_websocket_sequence_gaps_total",
            {"market": "nse"},
            absent_value=0,
        ),
        "cycle_errors": metric_sum(
            text,
            "nanodelta_runtime_cycle_duration_seconds_count",
            {"market": "nse", "result": "error"},
            absent_value=0,
        ),
    }


def database(args: argparse.Namespace) -> psycopg.Connection[dict[str, Any]]:
    dsn = read_nonempty(args.database_url_file, "database URL file")
    connection = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
    connection.execute("SET default_transaction_read_only = on")
    return connection


def fetch_one(
    connection: psycopg.Connection[dict[str, Any]], sql: str, values: Sequence[Any] = ()
) -> dict[str, Any]:
    row = connection.execute(sql, values).fetchone()
    if row is None:
        raise RuntimeError("authoritative query returned no row")
    return row


def feed_state(connection: psycopg.Connection[dict[str, Any]]) -> dict[str, Any]:
    return fetch_one(
        connection,
        "SELECT active_provider,state,connected_at,last_event_at,gap_count,failover_count,"
        "fallback_available,updated_at FROM control.realtime_feed_state WHERE market='nse'",
    )


def delta(after: float, before: float, name: str) -> float:
    result = after - before
    if result < 0:
        raise ScenarioFailureError(f"metric reset during measured window: {name}")
    return result


def dhan_history_readiness(
    args: argparse.Namespace, connection: psycopg.Connection[dict[str, Any]]
) -> dict[str, Any]:
    threshold = now() - timedelta(days=args.minimum_history_days)
    fresh_after = now() - timedelta(days=args.maximum_history_staleness_days)
    rows = connection.execute(
        "WITH required(timeframe) AS (VALUES ('5m'),('15m'),('30m'),('1h')), "
        "enabled AS (SELECT symbol FROM control.market_universe "
        "WHERE market='nse' AND enabled=true), "
        "history AS (SELECT symbol,timeframe,min(open_time) AS earliest,max(open_time) AS latest,"
        "count(*) AS candle_count FROM nse_silver.candles "
        "WHERE provider='dhan' AND is_settled=true GROUP BY symbol,timeframe), "
        "runs AS (SELECT DISTINCT ON (symbol,timeframe) symbol,timeframe,state,provider,"
        "finished_at "
        "FROM control.history_runs WHERE market='nse' ORDER BY symbol,timeframe,started_at DESC) "
        "SELECT e.symbol,r.timeframe,h.earliest,h.latest,coalesce(h.candle_count,0) "
        "AS candle_count,"
        "x.state AS run_state,x.provider AS run_provider,x.finished_at "
        "FROM enabled e CROSS JOIN required r LEFT JOIN history h USING(symbol,timeframe) "
        "LEFT JOIN runs x USING(symbol,timeframe) ORDER BY e.symbol,r.timeframe"
    ).fetchall()
    universe = fetch_one(
        connection,
        "SELECT count(*) AS total FROM control.market_universe WHERE market='nse' AND enabled=true",
    )
    if not rows or int(universe["total"]) == 0:
        raise ScenarioFailureError("configured NSE universe is empty", {"enabled_symbols": 0})
    failures: list[dict[str, Any]] = []
    minimum_candles = {
        "5m": args.minimum_candles_5m,
        "15m": args.minimum_candles_15m,
        "30m": args.minimum_candles_30m,
        "1h": args.minimum_candles_1h,
    }
    counts: dict[str, dict[str, Any]] = {
        timeframe: {"grains": 0, "ready": 0, "candles": 0} for timeframe in REQUIRED_TIMEFRAMES
    }
    for row in rows:
        timeframe = str(row["timeframe"])
        ready = (
            row["earliest"] is not None
            and row["latest"] is not None
            and row["earliest"] <= threshold
            and row["latest"] >= fresh_after
            and int(row["candle_count"]) >= minimum_candles[timeframe]
            and row["run_state"] == "SUCCEEDED"
            and row["run_provider"] == "dhan"
            and row["finished_at"] is not None
        )
        counts[timeframe]["grains"] += 1
        counts[timeframe]["candles"] += int(row["candle_count"])
        counts[timeframe]["ready"] += int(ready)
        if not ready and len(failures) < args.maximum_failure_samples:
            failures.append(
                {
                    "symbol": row["symbol"],
                    "timeframe": timeframe,
                    "earliest": iso(row["earliest"]),
                    "latest": iso(row["latest"]),
                    "candle_count": int(row["candle_count"]),
                    "history_state": row["run_state"],
                    "history_provider": row["run_provider"],
                }
            )
    ready_grains = sum(int(item["ready"]) for item in counts.values())
    measurements = {
        "provider_identity": args.dhan_provider_identity,
        "enabled_symbols": int(universe["total"]),
        "required_timeframes": list(REQUIRED_TIMEFRAMES),
        "required_grains": len(rows),
        "ready_grains": ready_grains,
        "minimum_history_days": args.minimum_history_days,
        "maximum_history_staleness_days": args.maximum_history_staleness_days,
        "minimum_settled_candles": minimum_candles,
        "coverage_cutoff": iso(threshold),
        "freshness_cutoff": iso(fresh_after),
        "by_timeframe": counts,
        "failure_samples": failures,
    }
    api_universe = api_json(args, "/api/nse/universe?enabled=true&limit=1000")
    measurements["authoritative_api_universe_total"] = int(api_universe["page"]["total"])
    measurements["authoritative_api"] = bool(api_universe["freshness"]["authoritative"])
    if measurements["authoritative_api_universe_total"] != int(universe["total"]):
        raise ScenarioFailureError("universe API and database totals differ", measurements)
    if ready_grains != len(rows):
        raise ScenarioFailureError(
            "not every NSE symbol/timeframe has current two-year Dhan evidence", measurements
        )
    return measurements


def truedata_realtime_soak(
    args: argparse.Namespace, connection: psycopg.Connection[dict[str, Any]]
) -> dict[str, Any]:
    before_feed = feed_state(connection)
    if before_feed["active_provider"] != "truedata" or before_feed["state"] != "HEALTHY":
        raise ScenarioFailureError(
            "TrueData must be the healthy active NSE provider before the soak", before_feed
        )
    before = runtime_metrics(args)
    samples = 0
    deadline = time.monotonic() + args.soak_duration
    after = before
    while time.monotonic() < deadline:
        time.sleep(min(args.sample_interval, max(0.0, deadline - time.monotonic())))
        after = runtime_metrics(args)
        samples += 1
    after_feed = feed_state(connection)
    measurements = {
        "provider_identity": args.truedata_provider_identity,
        "duration_seconds": args.soak_duration,
        "samples": samples,
        "event_delta": delta(
            after["truedata_events"], before["truedata_events"], "truedata events"
        ),
        "cycle_error_delta": delta(after["cycle_errors"], before["cycle_errors"], "cycle errors"),
        "sequence_gap_delta": delta(after["gaps"], before["gaps"], "sequence gaps"),
        "failover_delta": delta(after["failovers"], before["failovers"], "failovers"),
        "active_provider_after": after_feed["active_provider"],
        "feed_state_after": after_feed["state"],
        "last_event_at": iso(after_feed["last_event_at"]),
        "minimum_events": args.minimum_soak_events,
        "maximum_cycle_errors": args.maximum_soak_cycle_errors,
        "maximum_sequence_gaps": args.maximum_soak_sequence_gaps,
    }
    if (
        measurements["event_delta"] < args.minimum_soak_events
        or measurements["cycle_error_delta"] > args.maximum_soak_cycle_errors
        or measurements["sequence_gap_delta"] > args.maximum_soak_sequence_gaps
        or measurements["failover_delta"] != 0
        or after_feed["active_provider"] != "truedata"
        or after_feed["state"] != "HEALTHY"
    ):
        raise ScenarioFailureError("TrueData soak thresholds were not met", measurements)
    return measurements


def timescaledb_paper_lifecycle(
    args: argparse.Namespace, connection: psycopg.Connection[dict[str, Any]]
) -> dict[str, Any]:
    since = now() - timedelta(hours=args.lifecycle_since_hours)
    timescale = fetch_one(
        connection,
        "SELECT extversion FROM pg_extension WHERE extname='timescaledb'",
    )
    row = connection.execute(
        "SELECT c.candidate_id,c.cycle_id,c.symbol,c.timeframe,c.strategy_key,c.approval_id,"
        "c.action,c.event_time,c.gold_snapshot_ids,d.decision_id,o.order_id,o.execution_mode,"
        "f.fill_id,op.position_id,p.state AS position_state,p.closed_at,x.outcome_id,x.recorded_at,"
        "ep.state AS exit_plan_state,ep.exit_reason "
        "FROM control.signal_candidates c JOIN paper.decisions d ON d.candidate_id=c.candidate_id "
        "JOIN research.strategy_approvals a ON a.approval_id=d.approval_id "
        "JOIN research.validation_runs v ON v.validation_run_id=a.validation_run_id "
        "JOIN paper.orders o ON o.decision_id=d.decision_id "
        "JOIN paper.fills f ON f.order_id=o.order_id "
        "JOIN paper.order_positions op ON op.order_id=o.order_id "
        "JOIN paper.positions p ON p.position_id=op.position_id "
        "JOIN paper.outcomes x ON x.position_id=p.position_id "
        "JOIN paper.exit_plans ep ON ep.position_id=p.position_id "
        "WHERE c.market='nse' AND c.created_at >= %s AND d.state='APPROVED' "
        "AND v.passed=true AND a.approved_at <= c.event_time AND a.expires_at > c.event_time "
        "AND o.execution_mode='PAPER' AND p.state='CLOSED' AND ep.state='CLOSED' "
        "ORDER BY x.recorded_at DESC LIMIT 1",
        (since,),
    ).fetchone()
    measurements: dict[str, Any] = {
        "timescaledb_version": timescale["extversion"],
        "window_started_at": iso(since),
        "trading_mode": "PAPER",
        "completed_lifecycle_found": row is not None,
    }
    if row is None:
        raise ScenarioFailureError(
            "no completed authoritative NSE paper lifecycle was found", measurements
        )
    gold_ids = list(row["gold_snapshot_ids"])
    linked_gold = fetch_one(
        connection,
        "SELECT count(DISTINCT f.record_id) AS linked FROM nse_gold.feature_snapshots f "
        "WHERE f.record_id = ANY(%s)",
        (gold_ids,),
    )
    stages = connection.execute(
        "SELECT stage,count(*) AS total FROM control.decision_events "
        "WHERE market='nse' AND cycle_id=%s GROUP BY stage",
        (row["cycle_id"],),
    ).fetchall()
    stage_counts = {str(item["stage"]): int(item["total"]) for item in stages}
    required_stages = {"signal", "scoring", "portfolio_construction", "risk", "execution"}
    measurements.update(
        {
            "candidate_id": row["candidate_id"],
            "cycle_id": row["cycle_id"],
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "strategy_key": row["strategy_key"],
            "approval_id": row["approval_id"],
            "action": row["action"],
            "decision_id": row["decision_id"],
            "order_id": row["order_id"],
            "fill_id": row["fill_id"],
            "position_id": row["position_id"],
            "outcome_id": row["outcome_id"],
            "exit_reason": row["exit_reason"],
            "gold_snapshot_count": len(gold_ids),
            "linked_gold_snapshot_count": int(linked_gold["linked"]),
            "decision_stage_counts": stage_counts,
        }
    )
    encoded_cycle = urllib.parse.quote(str(row["cycle_id"]), safe="")
    api_signal = api_json(args, f"/api/nse/signals?cycle_id={encoded_cycle}&limit=500")
    api_orders = api_json(
        args, f"/api/nse/orders?symbol={urllib.parse.quote(str(row['symbol']))}&limit=500"
    )
    api_trades = api_json(
        args, f"/api/nse/trades?symbol={urllib.parse.quote(str(row['symbol']))}&limit=500"
    )
    measurements["authoritative_api_records"] = {
        "signals": int(api_signal["page"]["total"]),
        "orders": int(api_orders["page"]["total"]),
        "trades": int(api_trades["page"]["total"]),
    }
    if not gold_ids or int(linked_gold["linked"]) != len(set(gold_ids)):
        raise ScenarioFailureError("paper lifecycle Gold lineage is incomplete", measurements)
    if not required_stages.issubset(stage_counts):
        raise ScenarioFailureError(
            "paper lifecycle decision-stage lineage is incomplete", measurements
        )
    if any(value < 1 for value in measurements["authoritative_api_records"].values()):
        raise ScenarioFailureError(
            "paper lifecycle is not visible through authoritative APIs", measurements
        )
    return measurements


def runtime_snapshot(connection: psycopg.Connection[dict[str, Any]]) -> dict[str, Any]:
    runtime = fetch_one(
        connection,
        "SELECT instance_id,state,last_heartbeat,last_cycle_finished,updated_at "
        "FROM control.runtime_instances WHERE market='nse'",
    )
    feed = feed_state(connection)
    counts = fetch_one(
        connection,
        "SELECT (SELECT count(*) FROM control.signal_candidates WHERE market='nse') AS candidates,"
        "(SELECT count(*) FROM paper.orders WHERE market='nse') AS orders,"
        "(SELECT count(*) FROM paper.fills f JOIN paper.orders o ON o.order_id=f.order_id "
        "WHERE o.market='nse') AS fills,"
        "(SELECT count(*) FROM paper.outcomes WHERE market='nse') AS outcomes",
    )
    sequences = connection.execute(
        "SELECT provider,symbol,last_sequence,gap_count FROM control.realtime_sequence_state "
        "WHERE market='nse' ORDER BY provider,symbol"
    ).fetchall()
    return {"runtime": runtime, "feed": feed, "counts": counts, "sequences": sequences}


def compose_prefix(args: argparse.Namespace) -> list[str]:
    result = ["docker", "compose"]
    for compose_file in args.compose_file:
        result.extend(("-f", compose_file))
    return result


def runtime_restart_recovery(
    args: argparse.Namespace, connection: psycopg.Connection[dict[str, Any]]
) -> dict[str, Any]:
    if not args.allow_runtime_restart:
        raise ScenarioFailureError("--allow-runtime-restart is required")
    before = runtime_snapshot(connection)
    subprocess.run(  # noqa: S603
        [*compose_prefix(args), "--profile", "market-runtime", "restart", "runtime"],
        cwd=repository_root(),
        check=True,
    )
    deadline = time.monotonic() + args.restart_timeout
    after: dict[str, Any] | None = None
    post_restart_events = 0.0
    while time.monotonic() < deadline:
        try:
            candidate = runtime_snapshot(connection)
            metrics = runtime_metrics(args)
            post_restart_events = metrics["truedata_events"] + metrics["dhan_events"]
            heartbeat_advanced = (
                candidate["runtime"]["last_heartbeat"] > before["runtime"]["last_heartbeat"]
            )
            if (
                candidate["runtime"]["state"] == "RUNNING"
                and heartbeat_advanced
                and post_restart_events >= args.minimum_post_restart_events
            ):
                after = candidate
                break
        except Exception:
            pass
        time.sleep(args.sample_interval)
    measurements: dict[str, Any] = {
        "restart_timeout_seconds": args.restart_timeout,
        "minimum_post_restart_events": args.minimum_post_restart_events,
        "post_restart_events": post_restart_events,
        "recovered": after is not None,
        "before_heartbeat": iso(before["runtime"]["last_heartbeat"]),
        "after_heartbeat": iso(after["runtime"]["last_heartbeat"]) if after else None,
    }
    if after is None:
        raise ScenarioFailureError(
            "NSE runtime did not recover before the restart timeout", measurements
        )
    violations = fetch_one(
        connection,
        "SELECT (SELECT count(*)-count(DISTINCT idempotency_key) FROM paper.orders "
        "WHERE market='nse') AS duplicate_order_keys,"
        "(SELECT count(*)-count(DISTINCT f.order_id) FROM paper.fills f "
        "JOIN paper.orders o ON o.order_id=f.order_id WHERE o.market='nse') "
        "AS duplicate_order_fills,"
        "(SELECT count(*)-count(DISTINCT position_id) FROM paper.outcomes "
        "WHERE market='nse') AS duplicate_position_outcomes",
    )
    before_sequences = {
        (row["provider"], row["symbol"]): (int(row["last_sequence"]), int(row["gap_count"]))
        for row in before["sequences"]
    }
    after_sequences = {
        (row["provider"], row["symbol"]): (int(row["last_sequence"]), int(row["gap_count"]))
        for row in after["sequences"]
    }
    missing_or_regressed = [
        f"{provider}:{symbol}"
        for (provider, symbol), value in before_sequences.items()
        if (provider, symbol) not in after_sequences
        or after_sequences[(provider, symbol)][0] < value[0]
        or after_sequences[(provider, symbol)][1] < value[1]
    ]
    counts_before = {key: int(value) for key, value in before["counts"].items()}
    counts_after = {key: int(value) for key, value in after["counts"].items()}
    measurements.update(
        {
            "counts_before": counts_before,
            "counts_after": counts_after,
            "feed_failovers_before": int(before["feed"]["failover_count"]),
            "feed_failovers_after": int(after["feed"]["failover_count"]),
            "feed_gaps_before": int(before["feed"]["gap_count"]),
            "feed_gaps_after": int(after["feed"]["gap_count"]),
            "sequence_rows_before": len(before_sequences),
            "sequence_rows_after": len(after_sequences),
            "sequence_regression_samples": missing_or_regressed[: args.maximum_failure_samples],
            "idempotency_violations": {key: int(value) for key, value in violations.items()},
        }
    )
    if any(counts_after[key] < value for key, value in counts_before.items()):
        raise ScenarioFailureError(
            "durable NSE lifecycle counts regressed after restart", measurements
        )
    if missing_or_regressed or any(int(value) != 0 for value in violations.values()):
        raise ScenarioFailureError("restart persistence/idempotency checks failed", measurements)
    if int(after["feed"]["failover_count"]) < int(before["feed"]["failover_count"]) or int(
        after["feed"]["gap_count"]
    ) < int(before["feed"]["gap_count"]):
        raise ScenarioFailureError("durable feed counters regressed after restart", measurements)
    return measurements


def provider_failover(
    args: argparse.Namespace, connection: psycopg.Connection[dict[str, Any]]
) -> dict[str, Any]:
    if not args.allow_provider_interruption:
        raise ScenarioFailureError("--allow-provider-interruption is required")
    before_feed = feed_state(connection)
    before_metrics = runtime_metrics(args)
    if before_feed["active_provider"] != "truedata":
        raise ScenarioFailureError("TrueData must be active before the failover drill", before_feed)
    print("Apply the approved TrueData interruption now; waiting for NSE fallback evidence.")
    deadline = time.monotonic() + args.failover_timeout
    fallback_feed: dict[str, Any] | None = None
    fallback_metrics: dict[str, float] | None = None
    while time.monotonic() < deadline:
        candidate_feed = feed_state(connection)
        candidate_metrics = runtime_metrics(args)
        if (
            candidate_feed["active_provider"] == "dhan"
            and int(candidate_feed["failover_count"]) > int(before_feed["failover_count"])
            and candidate_metrics["dhan_events"] > before_metrics["dhan_events"]
        ):
            fallback_feed = candidate_feed
            fallback_metrics = candidate_metrics
            break
        time.sleep(args.sample_interval)
    measurements: dict[str, Any] = {
        "primary_provider_identity": args.truedata_provider_identity,
        "fallback_provider_identity": args.dhan_provider_identity,
        "failover_timeout_seconds": args.failover_timeout,
        "fallback_observed": fallback_feed is not None,
    }
    if fallback_feed is None or fallback_metrics is None:
        raise ScenarioFailureError(
            "approved TrueData interruption did not produce measured Dhan fallback", measurements
        )
    print("Restore TrueData now; waiting for healthy primary recovery.")
    recovery_deadline = time.monotonic() + args.primary_recovery_timeout
    recovered_feed: dict[str, Any] | None = None
    recovered_metrics: dict[str, float] | None = None
    while time.monotonic() < recovery_deadline:
        candidate_feed = feed_state(connection)
        candidate_metrics = runtime_metrics(args)
        if (
            candidate_feed["active_provider"] == "truedata"
            and candidate_feed["state"] == "HEALTHY"
            and candidate_metrics["truedata_events"] > fallback_metrics["truedata_events"]
        ):
            recovered_feed = candidate_feed
            recovered_metrics = candidate_metrics
            break
        time.sleep(args.sample_interval)
    measurements.update(
        {
            "failover_count_delta": int(fallback_feed["failover_count"])
            - int(before_feed["failover_count"]),
            "fallback_event_delta": fallback_metrics["dhan_events"] - before_metrics["dhan_events"],
            "fallback_state": fallback_feed["state"],
            "primary_recovery_timeout_seconds": args.primary_recovery_timeout,
            "primary_recovered": recovered_feed is not None,
            "primary_event_delta_after_restore": (
                recovered_metrics["truedata_events"] - fallback_metrics["truedata_events"]
                if recovered_metrics is not None
                else 0
            ),
        }
    )
    if recovered_feed is None:
        raise ScenarioFailureError(
            "TrueData did not recover as healthy active primary", measurements
        )
    return measurements


def invoke_shared(args: argparse.Namespace, scenario: str, extra: Sequence[str]) -> dict[str, Any]:
    root = repository_root()
    with tempfile.TemporaryDirectory(prefix="nanodelta-nse-acceptance-") as directory:
        evidence = Path(directory) / f"{scenario}.json"
        command = [
            sys.executable,
            str(root / "scripts/acceptance/run.py"),
            scenario,
            "--evidence",
            str(evidence),
            *extra,
        ]
        environment = dict(os.environ)
        environment.update(
            {
                "NANODELTA_ACCEPTANCE_EXTERNAL_CONFIRMED": "true",
                "NANODELTA_ACCEPTANCE_ENVIRONMENT": args.environment,
                "GITHUB_SHA": args.release_sha,
            }
        )
        completed = subprocess.run(command, cwd=root, env=environment, check=False)  # noqa: S603
        if not evidence.exists():
            raise ScenarioFailureError(f"shared {scenario} runner produced no evidence")
        payload = json.loads(evidence.read_text(encoding="utf-8"))
        if completed.returncode != 0 or payload.get("status") != "PASSED":
            raise ScenarioFailureError(
                f"shared {scenario} runner failed: {payload.get('reason', 'unknown')}",
                payload.get("measurements", {}),
            )
        if (
            payload.get("git_sha") != args.release_sha
            or payload.get("environment") != args.environment
        ):
            raise ScenarioFailureError(f"shared {scenario} evidence binding does not match")
        return dict(payload["measurements"])


def backup_restore(
    args: argparse.Namespace, connection: psycopg.Connection[dict[str, Any]]
) -> dict[str, Any]:
    del connection
    if not args.confirm_disposable_restore:
        raise ScenarioFailureError("--confirm-disposable-restore is required")
    extra: list[str] = [
        "--confirm-disposable-restore",
        "--destination",
        str(args.backup_destination),
        "--user",
        args.postgres_user,
        "--database",
        args.postgres_database,
    ]
    for compose_file in args.compose_file:
        extra.extend(("--compose-file", compose_file))
    measurements = invoke_shared(args, "backup-restore", extra)
    if not measurements.get("restore_target_disposable") or not measurements.get("sha256"):
        raise ScenarioFailureError("backup/restore evidence is incomplete", measurements)
    return measurements


def histogram_snapshot(args: argparse.Namespace) -> tuple[dict[float, float], float]:
    text = scrape_metrics(args)
    buckets: dict[float, float] = {}
    for labels, value in metric_samples(
        text, "nanodelta_decision_pipeline_duration_seconds_bucket"
    ):
        if labels.get("market") != "nse":
            continue
        upper = math.inf if labels.get("le") == "+Inf" else float(str(labels["le"]))
        buckets[upper] = buckets.get(upper, 0.0) + value
    if not buckets:
        raise RuntimeError("NSE decision-latency histogram is absent")
    errors = metric_sum(
        text,
        "nanodelta_decision_pipeline_duration_seconds_count",
        {"market": "nse", "result": "error"},
        absent_value=0,
    )
    return buckets, errors


def histogram_quantile(buckets: Mapping[float, float], quantile: float) -> float:
    total = buckets.get(math.inf, 0.0)
    if total <= 0:
        raise ScenarioFailureError("decision-latency window contains no samples")
    target = total * quantile
    for upper, count in sorted(buckets.items()):
        if count >= target:
            return upper
    return math.inf


def decision_latency(
    args: argparse.Namespace, connection: psycopg.Connection[dict[str, Any]]
) -> dict[str, Any]:
    del connection
    before, errors_before = histogram_snapshot(args)
    deadline = time.monotonic() + args.latency_duration
    while time.monotonic() < deadline:
        time.sleep(min(args.sample_interval, max(0.0, deadline - time.monotonic())))
    after, errors_after = histogram_snapshot(args)
    keys = set(before) | set(after)
    deltas = {key: after.get(key, 0.0) - before.get(key, 0.0) for key in keys}
    if any(value < 0 for value in deltas.values()):
        raise ScenarioFailureError("decision histogram reset during latency window")
    samples = deltas.get(math.inf, 0.0)
    p50 = histogram_quantile(deltas, 0.50)
    p95 = histogram_quantile(deltas, 0.95)
    error_delta = errors_after - errors_before
    measurements = {
        "duration_seconds": args.latency_duration,
        "samples": samples,
        "p50_upper_bound_seconds": p50 if math.isfinite(p50) else None,
        "p95_upper_bound_seconds": p95 if math.isfinite(p95) else None,
        "error_delta": error_delta,
        "minimum_samples": args.minimum_latency_samples,
        "maximum_p95_seconds": args.maximum_decision_p95,
        "maximum_errors": args.maximum_decision_errors,
        "metric": "nanodelta_decision_pipeline_duration_seconds",
    }
    if (
        samples < args.minimum_latency_samples
        or p95 > args.maximum_decision_p95
        or error_delta > args.maximum_decision_errors
    ):
        raise ScenarioFailureError("NSE decision-latency thresholds were not met", measurements)
    return measurements


def alertmanager_receipt(
    args: argparse.Namespace, connection: psycopg.Connection[dict[str, Any]]
) -> dict[str, Any]:
    del connection
    if not args.receipt_url:
        raise ScenarioFailureError("--receipt-url is required")
    measurements = invoke_shared(
        args,
        "alert-delivery",
        [
            "--alertmanager-url",
            args.alertmanager_url,
            "--receipt-url",
            args.receipt_url,
            "--delivery-timeout",
            str(args.alert_delivery_timeout),
            "--interval",
            str(args.sample_interval),
        ],
    )
    if measurements.get("receiver_acknowledged") is not True:
        raise ScenarioFailureError(
            "Alertmanager receiver did not acknowledge evidence ID", measurements
        )
    return measurements


Scenario = Callable[[argparse.Namespace, psycopg.Connection[dict[str, Any]]], dict[str, Any]]
RUNNERS: dict[str, Scenario] = {
    "dhan_history_readiness": dhan_history_readiness,
    "truedata_realtime_soak": truedata_realtime_soak,
    "timescaledb_paper_lifecycle": timescaledb_paper_lifecycle,
    "runtime_restart_recovery": runtime_restart_recovery,
    "provider_failover": provider_failover,
    "backup_restore": backup_restore,
    "decision_latency": decision_latency,
    "alertmanager_receipt": alertmanager_receipt,
}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("suite", choices=("suite",))
    result.add_argument("--evidence", type=Path, required=True)
    result.add_argument("--confirmation", type=Path, required=True)
    result.add_argument("--environment", required=True)
    result.add_argument("--release-sha", required=True)
    result.add_argument("--dhan-provider-identity", required=True)
    result.add_argument("--truedata-provider-identity", required=True)
    result.add_argument("--database-url-file", type=Path, required=True)
    result.add_argument("--api-key-file", type=Path, required=True)
    result.add_argument("--api-url", default="http://127.0.0.1:8000")
    result.add_argument("--metrics-url", default="http://127.0.0.1:9101/metrics")
    result.add_argument("--alertmanager-url", default="http://127.0.0.1:9093")
    result.add_argument("--receipt-url", default="")
    result.add_argument("--http-timeout", type=float, default=10)
    result.add_argument("--sample-interval", type=float, default=5)
    result.add_argument("--maximum-failure-samples", type=int, default=20)
    result.add_argument("--minimum-history-days", type=int, default=730)
    result.add_argument("--maximum-history-staleness-days", type=int, default=7)
    result.add_argument("--minimum-candles-5m", type=int, default=DEFAULT_MINIMUM_CANDLES["5m"])
    result.add_argument("--minimum-candles-15m", type=int, default=DEFAULT_MINIMUM_CANDLES["15m"])
    result.add_argument("--minimum-candles-30m", type=int, default=DEFAULT_MINIMUM_CANDLES["30m"])
    result.add_argument("--minimum-candles-1h", type=int, default=DEFAULT_MINIMUM_CANDLES["1h"])
    result.add_argument("--soak-duration", type=float, default=21600)
    result.add_argument("--minimum-soak-events", type=float, default=10000)
    result.add_argument("--maximum-soak-cycle-errors", type=float, default=0)
    result.add_argument("--maximum-soak-sequence-gaps", type=float, default=0)
    result.add_argument("--lifecycle-since-hours", type=float, default=24)
    result.add_argument("--allow-runtime-restart", action="store_true")
    result.add_argument("--restart-timeout", type=float, default=180)
    result.add_argument("--minimum-post-restart-events", type=float, default=10)
    result.add_argument("--allow-provider-interruption", action="store_true")
    result.add_argument("--failover-timeout", type=float, default=300)
    result.add_argument("--primary-recovery-timeout", type=float, default=600)
    result.add_argument("--confirm-disposable-restore", action="store_true")
    result.add_argument(
        "--backup-destination", type=Path, default=Path("/secure/nanodelta/backups")
    )
    result.add_argument("--compose-file", action="append", default=[])
    result.add_argument("--postgres-user", default=os.environ.get("POSTGRES_USER", "nanodelta"))
    result.add_argument("--postgres-database", default=os.environ.get("POSTGRES_DB", "nanodelta"))
    result.add_argument("--latency-duration", type=float, default=1800)
    result.add_argument("--minimum-latency-samples", type=float, default=100)
    result.add_argument("--maximum-decision-p95", type=float, default=1)
    result.add_argument("--maximum-decision-errors", type=float, default=0)
    result.add_argument("--alert-delivery-timeout", type=float, default=120)
    return result


def validate_args(args: argparse.Namespace, root: Path) -> tuple[Path, dict[str, Any]]:
    actual_sha = git_sha(root)
    if args.release_sha != actual_sha:
        raise ValueError("--release-sha must equal the checked-out repository HEAD")
    if not args.environment.strip() or args.environment.lower() in {"unknown", "test"}:
        raise ValueError("a specific non-test --environment is required")
    for label, identity in (
        ("Dhan", args.dhan_provider_identity),
        ("TrueData", args.truedata_provider_identity),
    ):
        if not identity.strip() or identity.lower() in {"unknown", "dhan", "truedata"}:
            raise ValueError(f"{label} requires a non-secret deployment identity alias")
    evidence = require_external_path(args.evidence, root, "evidence output")
    confirmation_path = require_external_path(args.confirmation, root, "confirmation manifest")
    confirmation = load_confirmation(
        confirmation_path, environment=args.environment, release_sha=args.release_sha
    )
    database_url_path = require_external_path(args.database_url_file, root, "database URL file")
    api_key_path = require_external_path(args.api_key_file, root, "API key file")
    require_external_path(args.backup_destination, root, "backup destination")
    if not database_url_path.is_file() or not api_key_path.is_file():
        raise ValueError("database URL and API key files must exist")
    if args.sample_interval <= 0:
        raise ValueError("--sample-interval must be positive")
    if args.soak_duration <= 0 or args.latency_duration <= 0:
        raise ValueError("soak and latency durations must be positive")
    return evidence, confirmation


def main() -> int:
    args = parser().parse_args()
    root = repository_root()
    started = now()
    evidence_path = args.evidence.expanduser().resolve()
    evidence_validated = False
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "FAILED",
        "execution_mode": "external",
        "trading_mode": "PAPER",
        "release_sha": args.release_sha,
        "environment": {"id": args.environment, "kind": "single-host-paper-production"},
        "providers": {
            "dhan": args.dhan_provider_identity,
            "truedata": args.truedata_provider_identity,
        },
        "started_at": iso(started),
        "finished_at": None,
        "operator_confirmation": None,
        "scenarios": {name: {"status": "NOT_RUN", "measurements": {}} for name in SCENARIOS},
        "reason": None,
    }
    current: str | None = None
    exit_code = 1
    try:
        evidence_path = require_external_path(args.evidence, root, "evidence output")
        evidence_validated = True
        evidence_path, confirmation = validate_args(args, root)
        payload["operator_confirmation"] = {
            "sha256": sha256(args.confirmation.expanduser().resolve()),
            "approved_by": confirmation["approved_by"],
            "approved_at": confirmation["approved_at"],
            "change_ticket": confirmation["change_ticket"],
            "scenario_references": {
                name: confirmation["scenarios"][name]["reference"] for name in SCENARIOS
            },
        }
        # Readiness is checked before any long-running or disruptive operation.
        api_ready = json.loads(
            fetch(args.api_url.rstrip("/") + "/health/ready", timeout=args.http_timeout)
        )
        if not isinstance(api_ready, dict) or api_ready.get("status") != "ready":
            raise RuntimeError("API readiness is not ready")
        with database(args) as connection:
            for current in SCENARIOS:
                scenario_started = now()
                try:
                    measurements = RUNNERS[current](args, connection)
                except ScenarioFailureError as exc:
                    payload["scenarios"][current] = {
                        "status": "FAILED",
                        "started_at": iso(scenario_started),
                        "finished_at": iso(now()),
                        "reason": safe_reason(exc),
                        "measurements": exc.measurements,
                    }
                    raise
                payload["scenarios"][current] = {
                    "status": "PASSED",
                    "started_at": iso(scenario_started),
                    "finished_at": iso(now()),
                    "measurements": measurements,
                }
        if not all(payload["scenarios"][name]["status"] == "PASSED" for name in SCENARIOS):
            raise RuntimeError("not every NSE acceptance scenario passed")
        payload["status"] = "PASSED"
        payload["reason"] = None
        exit_code = 0
    except Exception as exc:
        payload["status"] = "FAILED"
        payload["reason"] = safe_reason(exc)
        if current is not None and payload["scenarios"][current]["status"] == "NOT_RUN":
            payload["scenarios"][current] = {
                "status": "FAILED",
                "started_at": iso(started),
                "finished_at": iso(now()),
                "reason": safe_reason(exc),
                "measurements": {},
            }
        print(f"NSE production acceptance failed: {safe_reason(exc)}", file=sys.stderr)
    finally:
        payload["finished_at"] = iso(now())
        if evidence_validated:
            try:
                write_json(evidence_path, payload)
            except Exception as write_error:
                exit_code = 1
                print(
                    f"unable to write acceptance evidence: {safe_reason(write_error)}",
                    file=sys.stderr,
                )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
