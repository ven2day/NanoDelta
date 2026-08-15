"""Shared HTTP and reconnecting JSON WebSocket transports."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import websockets

from nanodelta.providers.base import HttpRequest, ProviderClientError, RealtimeSubscription


class HttpxJsonTransport:
    def __init__(self, *, timeout_seconds: float = 20.0, retries: int = 2) -> None:
        self._timeout = timeout_seconds
        self._retries = retries

    async def request(self, request: HttpRequest) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        request.method,
                        request.url,
                        headers=dict(request.headers),
                        params={key: str(value) for key, value in request.params.items()},
                        json=dict(request.json_body) if request.json_body is not None else None,
                    )
                    response.raise_for_status()
                    return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt == self._retries:
                    break
                await asyncio.sleep(0.25 * (2**attempt))
        raise ProviderClientError(f"HTTP provider request failed: {last_error}")


async def stream_json(
    subscription: RealtimeSubscription,
    parser: Callable[[Any], list[dict[str, Any]]],
    *,
    reconnects: int = 5,
) -> AsyncIterator[dict[str, Any]]:
    failure: Exception | None = None
    for attempt in range(reconnects + 1):
        try:
            async with websockets.connect(
                subscription.url,
                additional_headers=dict(subscription.headers) or None,
                ping_interval=20,
                ping_timeout=20,
            ) as socket:
                if subscription.subscribe is not None:
                    await socket.send(json.dumps(subscription.subscribe, separators=(",", ":")))
                async for message in socket:
                    decoded = json.loads(message) if isinstance(message, str) else message
                    for payload in parser(decoded):
                        yield payload
                return
        except Exception as exc:
            failure = exc
            if attempt == reconnects:
                break
            await asyncio.sleep(min(30.0, 0.5 * (2**attempt)))
    raise ProviderClientError(f"WebSocket provider stream failed: {failure}")


async def stream_http_json_lines(
    subscription: RealtimeSubscription,
    parser: Callable[[Any], list[dict[str, Any]]],
    *,
    reconnects: int = 5,
) -> AsyncIterator[dict[str, Any]]:
    failure: Exception | None = None
    for attempt in range(reconnects + 1):
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "GET", subscription.url, headers=dict(subscription.headers)
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        for payload in parser(json.loads(line)):
                            yield payload
                    return
        except (httpx.HTTPError, ValueError) as exc:
            failure = exc
            if attempt == reconnects:
                break
            await asyncio.sleep(min(30.0, 0.5 * (2**attempt)))
    raise ProviderClientError(f"HTTP provider stream failed: {failure}")
