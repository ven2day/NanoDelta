from __future__ import annotations

from typing import Any

import pytest

from nanodelta.providers.base import HttpRequest, ProviderClientError
from nanodelta.providers.dhan import DhanClient


class FakeTransport:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.requests: list[HttpRequest] = []

    async def request(self, request: HttpRequest) -> Any:
        self.requests.append(request)
        return self.response


def client(transport: FakeTransport) -> DhanClient:
    return DhanClient(
        client_id="client-1",
        access_token="token-1",
        security_ids={"RELIANCE": "2885", "TCS": "11536"},
        transport=transport,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_fetch_quotes_parses_circuit_limits_and_depth() -> None:
    transport = FakeTransport(
        {
            "data": {
                "NSE_EQ": {
                    "2885": {
                        "upper_circuit_limit": 1450.5,
                        "lower_circuit_limit": 1180.5,
                        "depth": {
                            "buy": [{"price": 1300.0, "quantity": 10, "orders": 1}],
                            "sell": [{"price": 1300.5, "quantity": 5, "orders": 1}],
                        },
                    }
                }
            },
            "status": "success",
        }
    )
    result = await client(transport).fetch_quotes(["RELIANCE"])
    snapshot = result["RELIANCE"]
    assert snapshot.upper_circuit_limit == 1450.5
    assert snapshot.lower_circuit_limit == 1180.5
    assert snapshot.best_bid == 1300.0
    assert snapshot.best_ask == 1300.5


@pytest.mark.asyncio
async def test_fetch_quotes_request_body_uses_security_ids() -> None:
    transport = FakeTransport({"data": {"NSE_EQ": {}}, "status": "success"})
    await client(transport).fetch_quotes(["RELIANCE", "TCS"])
    assert transport.requests[0].json_body == {"NSE_EQ": [2885, 11536]}


@pytest.mark.asyncio
async def test_fetch_quotes_skips_symbols_missing_circuit_limits() -> None:
    transport = FakeTransport({"data": {"NSE_EQ": {"2885": {"depth": {}}}}, "status": "success"})
    result = await client(transport).fetch_quotes(["RELIANCE"])
    assert result == {}


@pytest.mark.asyncio
async def test_fetch_quotes_handles_missing_depth_gracefully() -> None:
    transport = FakeTransport(
        {
            "data": {
                "NSE_EQ": {
                    "2885": {"upper_circuit_limit": 100.0, "lower_circuit_limit": 90.0},
                }
            },
            "status": "success",
        }
    )
    result = await client(transport).fetch_quotes(["RELIANCE"])
    snapshot = result["RELIANCE"]
    assert snapshot.best_bid is None
    assert snapshot.best_ask is None


@pytest.mark.asyncio
async def test_fetch_quotes_empty_symbols_makes_no_request() -> None:
    transport = FakeTransport({})
    result = await client(transport).fetch_quotes([])
    assert result == {}
    assert transport.requests == []


@pytest.mark.asyncio
async def test_fetch_quotes_rejects_non_object_response() -> None:
    transport = FakeTransport([1, 2, 3])
    with pytest.raises(ProviderClientError, match="must be an object"):
        await client(transport).fetch_quotes(["RELIANCE"])
