from __future__ import annotations

from typing import Any

import pytest

from nanodelta.providers.base import RealtimeSubscription
from nanodelta.providers.transports import stream_json


class FakeSocket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> str:
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)


class Connection:
    def __init__(self, socket: FakeSocket | None, error: Exception | None = None) -> None:
        self.socket, self.error = socket, error

    async def __aenter__(self) -> FakeSocket:
        if self.error:
            raise self.error
        assert self.socket is not None
        return self.socket

    async def __aexit__(self, *args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_json_transport_reconnects_and_restores_subscription(monkeypatch: Any) -> None:
    restored = FakeSocket(['{"data":[{"last":"10"}]}'])
    connections = iter([Connection(None, RuntimeError("lost")), Connection(restored)])
    monkeypatch.setattr(
        "nanodelta.providers.transports.websockets.connect",
        lambda *args, **kwargs: next(connections),
    )

    async def no_sleep(delay: float) -> None:
        assert delay >= 0

    monkeypatch.setattr("nanodelta.providers.transports.asyncio.sleep", no_sleep)
    subscription = RealtimeSubscription(
        "wss://fixture", {"op": "subscribe", "args": [{"channel": "tickers"}]}
    )
    received = [
        payload
        async for payload in stream_json(
            subscription,
            lambda message: message["data"],
            reconnects=1,
        )
    ]
    assert received == [{"last": "10"}]
    assert restored.sent == ['{"op":"subscribe","args":[{"channel":"tickers"}]}']
