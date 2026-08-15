"""Idempotent market-scoped runtime state machine with immutable audit records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from nanodelta.contracts import Market, stable_id, utc


class WorkerState(StrEnum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"


class Command(StrEnum):
    START = "start"
    STOP = "stop"
    DRAIN = "drain"
    REPAIR = "repair"


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: str

    @property
    def can_operate(self) -> bool:
        return self.role in {"operator", "admin"}


@dataclass(frozen=True)
class AuditRecord:
    audit_id: str
    idempotency_key: str
    market: Market
    command: Command
    actor_id: str
    previous_state: WorkerState
    resulting_state: WorkerState
    requested_at: datetime
    detail: str


class OperationalStore:
    """Authoritative in-memory read models; database adapters can hydrate the same shapes."""

    def __init__(self) -> None:
        self.workers = {market: WorkerState.STOPPED for market in Market}
        self.heartbeats: dict[Market, datetime] = {}
        self.provider_health: dict[Market, dict[str, Any]] = {market: {} for market in Market}
        self.features: dict[Market, list[dict[str, Any]]] = {market: [] for market in Market}
        self.strategies: dict[Market, list[dict[str, Any]]] = {market: [] for market in Market}
        self.agent_runs: dict[Market, list[dict[str, Any]]] = {market: [] for market in Market}
        self.decisions: dict[Market, list[dict[str, Any]]] = {market: [] for market in Market}
        self.positions: dict[Market, list[dict[str, Any]]] = {market: [] for market in Market}
        self.outcomes: dict[Market, list[dict[str, Any]]] = {market: [] for market in Market}
        self.audit: dict[str, AuditRecord] = {}

    def worker_state(self, market: Market) -> WorkerState:
        return self.workers[market]

    def set_worker_state(self, market: Market, state: WorkerState) -> None:
        self.workers[market] = state

    def audit_record(self, idempotency_key: str) -> AuditRecord | None:
        return self.audit.get(idempotency_key)

    def save_audit(self, record: AuditRecord) -> None:
        self.audit[record.idempotency_key] = record

    def commit_transition(self, record: AuditRecord) -> None:
        self.set_worker_state(record.market, record.resulting_state)
        self.save_audit(record)


class WorkerControl(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def drain(self) -> None: ...


class RuntimeController:
    def __init__(
        self,
        store: OperationalStore,
        workers: dict[Market, WorkerControl] | None = None,
    ) -> None:
        self.store = store
        self._workers = workers or {}

    def command(
        self,
        market: Market,
        command: Command,
        actor: Actor,
        *,
        idempotency_key: str,
        confirmed: bool,
        requested_at: datetime,
        detail: str = "",
    ) -> AuditRecord:
        if not actor.can_operate:
            raise PermissionError("operator or admin role is required")
        if not idempotency_key.strip():
            raise ValueError("idempotency key is required")
        existing = self.store.audit_record(idempotency_key)
        if existing is not None:
            if (
                existing.market is not market
                or existing.command is not command
                or existing.detail != detail
            ):
                raise ValueError("idempotency key is bound to another command")
            return existing
        if not confirmed:
            raise PermissionError("explicit confirmation is required")
        previous = self.store.worker_state(market)
        transitions = {
            Command.START: WorkerState.RUNNING,
            Command.STOP: WorkerState.STOPPED,
            Command.DRAIN: WorkerState.DRAINING,
            Command.REPAIR: previous,
        }
        if command is Command.DRAIN and previous is not WorkerState.RUNNING:
            raise RuntimeError("only a running worker can drain")
        if command is not Command.REPAIR:
            worker = self._workers.get(market)
            if worker is None:
                raise RuntimeError(f"{market.value} worker is not configured")
            {
                Command.START: worker.start,
                Command.STOP: worker.stop,
                Command.DRAIN: worker.drain,
            }[command]()
        resulting = transitions[command]
        requested_at = utc(requested_at, "requested_at")
        record = AuditRecord(
            stable_id(idempotency_key, market.value, command.value),
            idempotency_key,
            market,
            command,
            actor.actor_id,
            previous,
            resulting,
            requested_at,
            detail,
        )
        self.store.commit_transition(record)
        return record
