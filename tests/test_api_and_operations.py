from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from nanodelta.api import ApiServices, create_app
from nanodelta.contracts import Market
from nanodelta.decisions import (
    Decision,
    DecisionStage,
    DecisionStatus,
    InMemoryDecisionLedger,
)
from nanodelta.finops import (
    BillingMode,
    BudgetPolicy,
    FinOpsGuard,
    InMemoryFinOpsLedger,
    SubscriptionPlan,
)
from nanodelta.operations import Actor, Command, OperationalStore, RuntimeController, WorkerState


class FakeWorker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def drain(self) -> None:
        self.calls.append("drain")


def client() -> tuple[TestClient, OperationalStore]:
    store = OperationalStore()
    store.features[Market.NSE] = [{"record_id": "nse-feature"}]
    store.features[Market.FOREX] = [{"record_id": "forex-feature"}]
    workers = {market: FakeWorker() for market in Market}
    services = ApiServices(
        store,
        RuntimeController(store, workers),
        {},
        {},
        {"operator-key": Actor("operator-1", "operator"), "read-key": Actor("r", "viewer")},
    )
    return TestClient(create_app(services)), store


def test_read_endpoints_are_market_scoped_and_unknown_market_is_404() -> None:
    api, _ = client()

    headers = {"X-API-Key": "read-key"}
    assert api.get("/api/nse/features", headers=headers).json() == [{"record_id": "nse-feature"}]
    assert api.get("/api/forex/features", headers=headers).json() == [
        {"record_id": "forex-feature"}
    ]
    assert api.get("/api/crypto/features", headers=headers).json() == []
    assert api.get("/api/unknown/features", headers=headers).status_code == 404


def test_runtime_commands_require_auth_confirmation_and_are_idempotent() -> None:
    api, store = client()
    url = "/api/nse/runtime/start"

    assert (
        api.post(url, json={"confirmed": True}, headers={"Idempotency-Key": "1"}).status_code == 401
    )
    headers = {"X-API-Key": "operator-key", "Idempotency-Key": "start-1"}
    assert api.post(url, json={"confirmed": False}, headers=headers).status_code == 403
    first = api.post(url, json={"confirmed": True}, headers=headers)
    second = api.post(url, json={"confirmed": True}, headers=headers)

    assert first.status_code == 200
    assert second.json()["audit_id"] == first.json()["audit_id"]
    assert store.workers[Market.NSE] is WorkerState.RUNNING
    assert store.workers[Market.FOREX] is WorkerState.STOPPED


def test_read_role_cannot_operate_and_drain_requires_running_worker() -> None:
    api, _ = client()
    read_headers = {"X-API-Key": "read-key", "Idempotency-Key": "x"}
    assert (
        api.post(
            "/api/crypto/runtime/start",
            json={"confirmed": True},
            headers=read_headers,
        ).status_code
        == 403
    )
    operator = {"X-API-Key": "operator-key", "Idempotency-Key": "drain-1"}
    assert (
        api.post(
            "/api/crypto/runtime/drain",
            json={"confirmed": True},
            headers=operator,
        ).status_code
        == 409
    )


def test_controller_rejects_idempotency_key_reuse_across_commands() -> None:
    store = OperationalStore()
    controller = RuntimeController(store, {Market.NSE: FakeWorker()})
    actor = Actor("operator", "operator")
    controller.command(
        Market.NSE,
        Command.START,
        actor,
        idempotency_key="same",
        confirmed=True,
        requested_at=datetime.now(UTC),
    )
    try:
        controller.command(
            Market.NSE,
            Command.STOP,
            actor,
            idempotency_key="same",
            confirmed=True,
            requested_at=datetime.now(UTC),
        )
    except ValueError as exc:
        assert "another command" in str(exc)
    else:
        raise AssertionError("idempotency key reuse must fail")


def test_controller_does_not_fabricate_running_state_without_worker() -> None:
    store = OperationalStore()
    controller = RuntimeController(store)
    try:
        controller.command(
            Market.FOREX,
            Command.START,
            Actor("operator", "operator"),
            idempotency_key="missing-worker",
            confirmed=True,
            requested_at=datetime.now(UTC),
        )
    except RuntimeError as exc:
        assert "not configured" in str(exc)
    else:
        raise AssertionError("unconfigured worker must fail")
    assert store.workers[Market.FOREX] is WorkerState.STOPPED


def test_finops_status_and_kill_switch_require_admin() -> None:
    store = OperationalStore()
    guard = FinOpsGuard(
        provider="qwen",
        billing_mode=BillingMode.SUBSCRIPTION,
        policy=BudgetPolicy(100, 100_000, Decimal("0")),
        ledger=InMemoryFinOpsLedger(),
        subscription=SubscriptionPlan("configured-plan", Decimal("25")),
    )
    services = ApiServices(
        store,
        RuntimeController(store),
        {},
        {},
        {
            "operator-key": Actor("operator-1", "operator"),
            "admin-key": Actor("admin-1", "admin"),
            "viewer-key": Actor("viewer-1", "viewer"),
        },
        finops=guard,
    )
    api = TestClient(create_app(services))

    status = api.get("/api/finops", headers={"X-API-Key": "viewer-key"})
    assert status.status_code == 200
    assert status.json()["billing_mode"] == "SUBSCRIPTION"
    assert status.json()["subscription_monthly_fee_usd"] == "25"

    body = {"active": True, "reason": "maintenance"}
    assert (
        api.post(
            "/api/finops/kill-switch",
            json=body,
            headers={"X-API-Key": "operator-key"},
        ).status_code
        == 403
    )
    response = api.post(
        "/api/finops/kill-switch",
        json=body,
        headers={"X-API-Key": "admin-key"},
    )
    assert response.status_code == 200
    assert response.json() == {"active": True, "reason": "admin-1: maintenance"}


def test_decision_cycle_endpoint_returns_queryable_stage_records() -> None:
    store = OperationalStore()
    ledger = InMemoryDecisionLedger()
    ledger.append(
        Decision.create(
            cycle_id="cycle-1",
            market=Market.NSE,
            symbol="RELIANCE",
            timeframe="15m",
            stage=DecisionStage.SCORING,
            status=DecisionStatus.REJECTED,
            reason_code="NON_POSITIVE_EXPECTED_R",
            occurred_at=datetime.now(UTC),
        )
    )
    api = TestClient(
        create_app(
            ApiServices(
                store,
                RuntimeController(store),
                {},
                {},
                {"viewer-key": Actor("viewer", "viewer")},
                decision_ledger=ledger,
            )
        )
    )

    response = api.get("/api/decision-cycles/cycle-1", headers={"X-API-Key": "viewer-key"})
    assert response.status_code == 200
    assert response.json()[0]["reason_code"] == "NON_POSITIVE_EXPECTED_R"
