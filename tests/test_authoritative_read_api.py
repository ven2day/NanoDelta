from datetime import UTC, datetime

from fastapi.testclient import TestClient

from nanodelta.api import ApiServices, create_app
from nanodelta.api.read_models import InMemoryAuthoritativeReadStore
from nanodelta.operations import Actor, OperationalStore, RuntimeController


def api() -> TestClient:
    operations = OperationalStore()
    reads = InMemoryAuthoritativeReadStore()
    now = datetime(2026, 8, 15, 14, 0, tzinfo=UTC)
    reads.seed(
        "candles",
        [
            {
                "market": "nse",
                "symbol": "RELIANCE",
                "timeframe": "15m",
                "open_time": now,
                "close": 1500.0,
            },
            {
                "market": "nse",
                "symbol": "SBIN",
                "timeframe": "15m",
                "open_time": now,
                "close": 800.0,
            },
        ],
    )
    reads.seed(
        "positions",
        [
            {
                "market": "nse",
                "symbol": "RELIANCE",
                "state": "OPEN",
                "signed_quantity": 2,
                "average_entry_price": 100.0,
                "realized_pnl": 5.0,
                "total_fees": 1.0,
                "updated_at": now,
            },
        ],
    )
    reads.seed(
        "trades",
        [
            {
                "market": "nse",
                "symbol": "SBIN",
                "net_pnl": 10.0,
                "gross_pnl": 12.0,
                "total_fees": 2.0,
                "recorded_at": now,
            },
            {
                "market": "nse",
                "symbol": "INFY",
                "net_pnl": -4.0,
                "gross_pnl": -3.0,
                "total_fees": 1.0,
                "recorded_at": now,
            },
        ],
    )
    return TestClient(
        create_app(
            ApiServices(
                operations,
                RuntimeController(operations),
                {},
                {},
                {
                    "viewer": Actor("v", "viewer"),
                    "operator": Actor("o", "operator"),
                    "admin": Actor("a", "admin"),
                },
                read_store=reads,
            )
        )
    )


def test_authoritative_reads_require_role_and_filter_and_page() -> None:
    client = api()
    url = "/api/nse/candles?symbol=RELIANCE&timeframe=15m&limit=1"
    assert client.get(url).status_code == 401
    response = client.get(url, headers={"X-API-Key": "viewer"})
    assert response.status_code == 200
    assert response.json()["items"][0]["symbol"] == "RELIANCE"
    assert response.json()["page"] == {"limit": 1, "offset": 0, "total": 1}
    assert response.json()["freshness"]["authoritative"] is True


def test_risk_and_performance_use_only_persisted_records() -> None:
    client = api()
    headers = {"X-API-Key": "viewer"}
    risk = client.get("/api/nse/risk/aggregate", headers=headers).json()
    assert risk["gross_entry_notional"] == 200.0
    assert risk["unrealized_pnl"] is None
    assert "unrealized_pnl" in risk["unavailable_fields"]
    performance = client.get("/api/nse/performance", headers=headers).json()
    assert performance["closed_trades"] == 2
    assert performance["net_pnl"] == 6.0
    assert performance["win_rate"] == 0.5


def test_unconfigured_read_model_fails_explicitly() -> None:
    operations = OperationalStore()
    client = TestClient(
        create_app(
            ApiServices(
                operations, RuntimeController(operations), {}, {}, {"viewer": Actor("v", "viewer")}
            )
        )
    )
    response = client.get(
        "/api/nse/candles?symbol=SBIN&timeframe=5m", headers={"X-API-Key": "viewer"}
    )
    assert response.status_code == 501
    assert response.json()["detail"]["code"] == "AUTHORITATIVE_READ_MODEL_UNAVAILABLE"


def test_viewer_cannot_operate_but_operator_can_reach_controller() -> None:
    client = api()
    request = {"confirmed": True}
    viewer = {"X-API-Key": "viewer", "Idempotency-Key": "viewer-start"}
    assert client.post("/api/nse/runtime/start", json=request, headers=viewer).status_code == 403
