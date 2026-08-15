"""OANDA v20 practice-first market-data and gated execution provider.

All public results are broker-neutral domain models.  Authentication values are kept
in private attributes and are never included in exceptions, logs, metrics, or reprs.
Only idempotent reads are retried automatically.  Order creation is available solely
behind the explicit OANDA-practice gate and is not wired into the live runtime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import aiohttp
from pydantic import SecretStr

from src.config.secrets import redact_secrets
from src.core.market_data import (
    RawEventSink,
    RawEventType,
    RawMarketEvent,
    emit_raw_event,
    validate_canonical_candle,
    validate_canonical_quote,
)
from src.core.models import (
    CanonicalCandle,
    CanonicalQuote,
    Instrument,
    Market,
    MarketProvider,
    ProviderHealth,
    VolumeType,
)

logger = logging.getLogger(__name__)

OANDA_TIMEFRAME_MAP: dict[str, str] = {
    "5m": "M5",
    "15m": "M15",
    "30m": "M30",
    "1h": "H1",
    "4h": "H4",
    "1d": "D",
}


class OandaEnvironment(StrEnum):
    PRACTICE = "practice"
    LIVE = "live"

    @classmethod
    def parse(cls, value: OandaEnvironment | str) -> OandaEnvironment:
        if isinstance(value, cls):
            return value
        return cls(str(value).strip().lower())

    @property
    def rest_url(self) -> str:
        if self is OandaEnvironment.PRACTICE:
            return "https://api-fxpractice.oanda.com"
        return "https://api-fxtrade.oanda.com"

    @property
    def stream_url(self) -> str:
        if self is OandaEnvironment.PRACTICE:
            return "https://stream-fxpractice.oanda.com"
        return "https://stream-fxtrade.oanda.com"


class OandaStreamHealthState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    CONNECTING = "CONNECTING"
    HEALTHY = "HEALTHY"
    STALE = "STALE"
    RECONNECTING = "RECONNECTING"
    AUTH_FAILED = "AUTH_FAILED"
    UNHEALTHY = "UNHEALTHY"


class OandaProviderError(RuntimeError):
    """Sanitized provider failure safe for logs and health APIs."""


class OandaAuthenticationError(OandaProviderError):
    pass


class OandaRateLimitError(OandaProviderError):
    pass


class OandaExecutionDisabledError(OandaProviderError):
    pass


@dataclass
class OandaMetrics:
    requests: int = 0
    retries: int = 0
    rate_limits: int = 0
    errors: int = 0
    stream_connections: int = 0
    stream_reconnects: int = 0
    heartbeats: int = 0
    prices: int = 0
    request_latency_ms: list[float] = field(default_factory=list, repr=False)

    def snapshot(self) -> dict[str, int | float]:
        latency = self.request_latency_ms[-100:]
        return {
            "requests": self.requests,
            "retries": self.retries,
            "rate_limits": self.rate_limits,
            "errors": self.errors,
            "stream_connections": self.stream_connections,
            "stream_reconnects": self.stream_reconnects,
            "heartbeats": self.heartbeats,
            "prices": self.prices,
            "average_request_latency_ms": sum(latency) / len(latency) if latency else 0.0,
        }


def _timestamp(value: str) -> datetime:
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def oanda_granularity(timeframe: str) -> str:
    try:
        return OANDA_TIMEFRAME_MAP[timeframe.strip().lower()]
    except KeyError as exc:
        supported = ", ".join(OANDA_TIMEFRAME_MAP)
        raise ValueError(f"Unsupported DeltaQuant timeframe {timeframe!r}; expected {supported}") from exc


def _first_price(levels: Any) -> tuple[float, float | None]:
    if not isinstance(levels, list) or not levels:
        raise ValueError("OANDA price payload has no liquidity level")
    level = levels[0]
    return float(level["price"]), float(level["liquidity"]) if level.get("liquidity") else None


def normalize_oanda_price(payload: dict[str, Any]) -> CanonicalQuote:
    bid, bid_liquidity = _first_price(payload.get("bids"))
    ask, ask_liquidity = _first_price(payload.get("asks"))
    quote = CanonicalQuote(
        symbol=str(payload["instrument"]).upper(),
        market=Market.FOREX,
        provider=MarketProvider.OANDA,
        timestamp=_timestamp(str(payload["time"])),
        bid=bid,
        ask=ask,
        tradeable=bool(payload.get("tradeable", True)),
        status=str(payload.get("status", "tradeable")).lower(),
        source="oanda_pricing",
        liquidity_bid=bid_liquidity,
        liquidity_ask=ask_liquidity,
    )
    return validate_canonical_quote(quote).require_valid()


def normalize_oanda_candle(
    instrument: str,
    timeframe: str,
    payload: dict[str, Any],
) -> CanonicalCandle:
    prices = payload.get("mid") or payload.get("bid") or payload.get("ask")
    if not isinstance(prices, dict):
        raise ValueError("OANDA candle has no mid/bid/ask OHLC payload")
    candle = CanonicalCandle(
        symbol=instrument.upper(),
        market=Market.FOREX,
        provider=MarketProvider.OANDA,
        timeframe=timeframe.lower(),
        timestamp=_timestamp(str(payload["time"])),
        open=float(prices["o"]),
        high=float(prices["h"]),
        low=float(prices["l"]),
        close=float(prices["c"]),
        volume=float(payload.get("volume", 0)),
        complete=bool(payload.get("complete", False)),
        source="oanda_candles",
        volume_type=VolumeType.OANDA_TICK_COUNT,
    )
    return validate_canonical_candle(candle).require_valid()


class OandaV20Client:
    """Persistent pooled OANDA v20 REST/stream client."""

    def __init__(
        self,
        *,
        environment: OandaEnvironment | str,
        account_id: SecretStr | str,
        access_token: SecretStr | str,
        rest_url_override: str = "",
        stream_url_override: str = "",
        timeout_seconds: float = 15.0,
        max_read_retries: int = 3,
        session: aiohttp.ClientSession | Any | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        raw_event_sink: RawEventSink | None = None,
    ) -> None:
        self.environment = OandaEnvironment.parse(environment)
        self._account_id = (
            account_id.get_secret_value() if isinstance(account_id, SecretStr) else str(account_id)
        ).strip()
        self._access_token = (
            access_token.get_secret_value()
            if isinstance(access_token, SecretStr)
            else str(access_token)
        ).strip()
        self.rest_url = (rest_url_override or self.environment.rest_url).rstrip("/")
        self.stream_url = (stream_url_override or self.environment.stream_url).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_read_retries = max_read_retries
        self._session = session
        self._owns_session = session is None
        self._sleep = sleep
        self._raw_event_sink = raw_event_sink
        self.metrics = OandaMetrics()
        self.last_stream_message_at: datetime | None = None
        self.last_stream_heartbeat_at: datetime | None = None
        self.last_stream_price_at: datetime | None = None
        self.stream_state = OandaStreamHealthState.NOT_STARTED
        self.stream_error: str | None = None
        self._closed = False

    def __repr__(self) -> str:
        return (
            f"OandaV20Client(environment={self.environment.value!r}, "
            "account_id='********', access_token='********')"
        )

    @property
    def account_id_configured(self) -> bool:
        return bool(self._account_id)

    @property
    def token_configured(self) -> bool:
        return bool(self._access_token)

    def set_raw_event_sink(self, sink: RawEventSink | None) -> None:
        """Attach Bronze persistence after the market database is initialized."""

        self._raw_event_sink = sink

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300, keepalive_timeout=30)
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return self._session

    def _headers(self) -> dict[str, str]:
        if not self._access_token:
            raise OandaAuthenticationError("OANDA access token is not configured")
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept-Datetime-Format": "RFC3339",
            "User-Agent": "DeltaQuant/2 OANDA-v20",
        }

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        retry_safe: bool = True,
    ) -> dict[str, Any]:
        if self._closed:
            raise OandaProviderError("OANDA client is closed")
        session = await self._get_session()
        attempts = self.max_read_retries + 1 if retry_safe else 1
        for attempt in range(attempts):
            started = time.perf_counter()
            self.metrics.requests += 1
            try:
                async with session.request(
                    method,
                    f"{self.rest_url}{path}",
                    headers=self._headers(),
                    params=params,
                    json=payload,
                ) as response:
                    body = await response.text()
                    self.metrics.request_latency_ms.append((time.perf_counter() - started) * 1000)
                    if response.status in {401, 403}:
                        raise OandaAuthenticationError(
                            f"OANDA authentication failed ({response.status}) for {method} {path}"
                        )
                    if response.status == 429:
                        self.metrics.rate_limits += 1
                        if attempt + 1 >= attempts:
                            raise OandaRateLimitError(f"OANDA rate limit for {method} {path}")
                        try:
                            retry_after_value = float(response.headers.get("Retry-After", 1))
                        except (TypeError, ValueError):
                            retry_after_value = 1.0
                        retry_after = min(30.0, max(0.1, retry_after_value))
                        self.metrics.retries += 1
                        await self._sleep(retry_after)
                        continue
                    if response.status >= 500 and retry_safe and attempt + 1 < attempts:
                        self.metrics.retries += 1
                        await self._sleep(min(8.0, (2**attempt) + random.random() * 0.25))
                        continue
                    if response.status >= 400:
                        message = ""
                        try:
                            decoded = json.loads(body)
                            message = str(redact_secrets(decoded).get("errorMessage", ""))[:160]
                        except (json.JSONDecodeError, AttributeError):
                            pass
                        suffix = f": {message}" if message else ""
                        raise OandaProviderError(
                            f"OANDA request failed ({response.status}) for {method} {path}{suffix}"
                        )
                    decoded = json.loads(body or "{}")
                    if not isinstance(decoded, dict):
                        raise OandaProviderError("OANDA returned a non-object JSON response")
                    return decoded
            except (OandaAuthenticationError, OandaRateLimitError, OandaProviderError):
                self.metrics.errors += 1
                raise
            except (TimeoutError, aiohttp.ClientError) as exc:
                if retry_safe and attempt + 1 < attempts:
                    self.metrics.retries += 1
                    await self._sleep(min(8.0, (2**attempt) + random.random() * 0.25))
                    continue
                self.metrics.errors += 1
                raise OandaProviderError(
                    f"OANDA transport failure for {method} {path}: {type(exc).__name__}"
                ) from exc
        raise OandaProviderError(f"OANDA request exhausted retries for {method} {path}")

    async def list_accounts(self) -> list[dict[str, Any]]:
        return list((await self._request_json("GET", "/v3/accounts")).get("accounts", []))

    async def validate_account(self) -> dict[str, Any]:
        if not self._account_id:
            raise OandaAuthenticationError("OANDA account ID is not configured")
        accounts = await self.list_accounts()
        authorized_ids = {str(item.get("id", "")) for item in accounts}
        if self._account_id not in authorized_ids:
            raise OandaAuthenticationError("Configured OANDA account is not authorized by this token")
        return await self.account_summary()

    async def account_summary(self) -> dict[str, Any]:
        response = await self._request_json(
            "GET", f"/v3/accounts/{self._account_id}/summary"
        )
        account = response.get("account", {})
        return dict(account) if isinstance(account, dict) else {}

    async def instruments(self) -> list[dict[str, Any]]:
        result = await self._request_json("GET", f"/v3/accounts/{self._account_id}/instruments")
        return list(result.get("instruments", []))

    async def candles(
        self,
        instrument: str,
        timeframe: str,
        *,
        count: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        complete_only: bool = True,
    ) -> list[CanonicalCandle]:
        if count is not None and (start is not None or end is not None):
            raise ValueError("OANDA candle count cannot be combined with start/end")
        # Pin alignment to UTC so 30m/1h/4h bars remain reproducible across DST
        # transitions and independent of OANDA account defaults.
        params: dict[str, Any] = {
            "granularity": oanda_granularity(timeframe),
            "price": "M",
            "alignmentTimezone": "UTC",
            "dailyAlignment": 0,
        }
        if count is not None:
            params["count"] = max(1, min(5000, int(count)))
        if start is not None:
            params["from"] = start.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if end is not None:
            params["to"] = end.astimezone(UTC).isoformat().replace("+00:00", "Z")
        result = await self._request_json(
            "GET", f"/v3/instruments/{instrument.upper()}/candles", params=params
        )
        raw_candles = result.get("candles", [])
        for item in raw_candles:
            if not isinstance(item, dict):
                continue
            source_time = _timestamp(str(item["time"]))
            emit_raw_event(
                self._raw_event_sink,
                RawMarketEvent.create(
                    market=Market.FOREX,
                    provider=MarketProvider.OANDA,
                    event_type=RawEventType.CANDLE,
                    symbol=instrument,
                    channel=f"candles:{timeframe}",
                    source_event_time=source_time,
                    payload=item,
                ),
            )
        candles = [normalize_oanda_candle(instrument, timeframe, item) for item in raw_candles]
        return [item for item in candles if item.complete] if complete_only else candles

    async def pricing(self, instruments: Sequence[str]) -> dict[str, CanonicalQuote]:
        if not instruments:
            return {}
        result = await self._request_json(
            "GET",
            f"/v3/accounts/{self._account_id}/pricing",
            params={"instruments": ",".join(dict.fromkeys(item.upper() for item in instruments))},
        )
        raw_prices = result.get("prices", [])
        for item in raw_prices:
            if not isinstance(item, dict):
                continue
            emit_raw_event(
                self._raw_event_sink,
                RawMarketEvent.create(
                    market=Market.FOREX,
                    provider=MarketProvider.OANDA,
                    event_type=RawEventType.QUOTE,
                    symbol=str(item["instrument"]),
                    channel="pricing_rest",
                    source_event_time=_timestamp(str(item["time"])),
                    payload=item,
                ),
            )
        quotes = [normalize_oanda_price(item) for item in raw_prices]
        return {item.symbol: item for item in quotes}

    async def pending_orders(self) -> list[dict[str, Any]]:
        result = await self._request_json("GET", f"/v3/accounts/{self._account_id}/pendingOrders")
        return list(result.get("orders", []))

    async def open_trades(self) -> list[dict[str, Any]]:
        result = await self._request_json("GET", f"/v3/accounts/{self._account_id}/openTrades")
        return list(result.get("trades", []))

    async def open_positions(self) -> list[dict[str, Any]]:
        result = await self._request_json("GET", f"/v3/accounts/{self._account_id}/openPositions")
        return list(result.get("positions", []))

    async def transactions_since(self, transaction_id: str) -> list[dict[str, Any]]:
        result = await self._request_json(
            "GET",
            f"/v3/accounts/{self._account_id}/transactions/sinceid",
            params={"id": transaction_id},
        )
        return list(result.get("transactions", []))

    async def create_order(self, order: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json(
            "POST",
            f"/v3/accounts/{self._account_id}/orders",
            payload={"order": order},
            retry_safe=False,
        )

    async def close_position(self, instrument: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json(
            "PUT",
            f"/v3/accounts/{self._account_id}/positions/{instrument.upper()}/close",
            payload=payload,
            retry_safe=False,
        )

    async def stream_messages(self, instruments: Sequence[str]) -> AsyncIterator[dict[str, Any]]:
        if not instruments:
            return
        reconnect_attempt = 0
        while not self._closed:
            self.stream_state = (
                OandaStreamHealthState.CONNECTING
                if reconnect_attempt == 0
                else OandaStreamHealthState.RECONNECTING
            )
            session = await self._get_session()
            try:
                async with session.get(
                    f"{self.stream_url}/v3/accounts/{self._account_id}/pricing/stream",
                    headers=self._headers(),
                    params={"instruments": ",".join(dict.fromkeys(item.upper() for item in instruments))},
                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=self.timeout_seconds),
                ) as response:
                    if response.status in {401, 403}:
                        self.stream_state = OandaStreamHealthState.AUTH_FAILED
                        raise OandaAuthenticationError("OANDA pricing-stream authentication failed")
                    if response.status >= 400:
                        raise OandaProviderError(
                            f"OANDA pricing stream failed with HTTP {response.status}"
                        )
                    self.metrics.stream_connections += 1
                    reconnect_attempt = 0
                    buffer = b""
                    async for chunk in response.content.iter_any():
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            if not line.strip():
                                continue
                            message = json.loads(line)
                            if not isinstance(message, dict):
                                continue
                            now = datetime.now(UTC)
                            self.last_stream_message_at = now
                            self.stream_state = OandaStreamHealthState.HEALTHY
                            self.stream_error = None
                            if message.get("type") == "HEARTBEAT":
                                self.last_stream_heartbeat_at = now
                                self.metrics.heartbeats += 1
                            elif message.get("type") == "PRICE":
                                self.last_stream_price_at = now
                                emit_raw_event(
                                    self._raw_event_sink,
                                    RawMarketEvent.create(
                                        market=Market.FOREX,
                                        provider=MarketProvider.OANDA,
                                        event_type=RawEventType.QUOTE,
                                        symbol=str(message["instrument"]),
                                        channel="pricing_stream",
                                        source_event_time=_timestamp(str(message["time"])),
                                        received_at=now,
                                        payload=message,
                                    ),
                                )
                            yield message
                    if not self._closed:
                        raise OandaProviderError("OANDA pricing stream ended")
            except asyncio.CancelledError:
                raise
            except OandaAuthenticationError:
                self.stream_state = OandaStreamHealthState.AUTH_FAILED
                raise
            except (TimeoutError, aiohttp.ClientError, OandaProviderError, json.JSONDecodeError) as exc:
                if self._closed:
                    break
                reconnect_attempt += 1
                self.metrics.stream_reconnects += 1
                self.stream_state = OandaStreamHealthState.RECONNECTING
                message = str(exc)
                if isinstance(exc, OandaProviderError) and "HTTP " in message:
                    self.stream_error = f"HTTP_{message.rsplit('HTTP ', 1)[-1].split()[0]}"
                else:
                    self.stream_error = type(exc).__name__
                delay = min(30.0, (2 ** min(reconnect_attempt, 5)) + random.random())
                logger.warning(
                    "OANDA pricing stream disconnected (%s); reconnecting in %.1fs",
                    type(exc).__name__,
                    delay,
                )
                await self._sleep(delay)

    async def close(self) -> None:
        self._closed = True
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._session = None
        self.stream_state = OandaStreamHealthState.NOT_STARTED


class OandaMarketDataProvider:
    def __init__(
        self,
        client: OandaV20Client,
        *,
        enabled_symbols: Sequence[str] = (),
        stale_seconds: float = 15.0,
    ) -> None:
        self.client = client
        self.enabled_symbols = tuple(dict.fromkeys(item.strip().upper() for item in enabled_symbols if item.strip()))
        self.stale_seconds = stale_seconds
        self._instruments: dict[str, Instrument] = {}
        self._latest_quotes: dict[str, CanonicalQuote] = {}
        self._stream_task: asyncio.Task[None] | None = None
        self._stream_message_event = asyncio.Event()

    async def list_instruments(self) -> list[Instrument]:
        raw = await self.client.instruments()
        configured = set(self.enabled_symbols)
        output: list[Instrument] = []
        for item in raw:
            symbol = str(item.get("name", "")).upper()
            if configured and symbol not in configured:
                continue
            base, _, quote = symbol.partition("_")
            instrument = Instrument(
                market=Market.FOREX,
                symbol=symbol,
                broker_symbol=symbol,
                provider=MarketProvider.OANDA,
                base_currency=base,
                quote_currency=quote,
                display_precision=int(item.get("displayPrecision", 5)),
                pip_location=int(item.get("pipLocation", -4)),
                trade_units_precision=int(item.get("tradeUnitsPrecision", 0)),
                minimum_trade_size=float(item.get("minimumTradeSize", 1)),
                maximum_order_units=(
                    float(item["maximumOrderUnits"]) if item.get("maximumOrderUnits") else None
                ),
                margin_rate=float(item["marginRate"]) if item.get("marginRate") else None,
                status=str(item.get("type", "CURRENCY")),
                tradeable=str(item.get("type", "")).upper() == "CURRENCY",
                metadata={"financing": redact_secrets(item.get("financing", {}))},
            )
            self._instruments[symbol] = instrument
            output.append(instrument)
        return output

    async def validate_connectivity(self) -> ProviderHealth:
        try:
            await self.client.validate_account()
            instruments = await self.list_instruments()
            if not instruments:
                raise OandaProviderError("No configured tradeable Forex instruments are available")
            quotes = await self.get_prices([instruments[0].symbol])
            if not quotes:
                raise OandaProviderError("OANDA pricing check returned no quote")
            latest = max(quotes.values(), key=lambda item: item.timestamp)
            age = latest.age_seconds()
            fresh = age <= self.stale_seconds
            return ProviderHealth(
                market=Market.FOREX,
                provider=MarketProvider.OANDA,
                # Startup health is a *connectivity* gate: authentication, the
                # configured instrument set, and a live pricing response all
                # succeeded.  A momentarily stale last tick (quiet liquidity,
                # session rollover, weekend) must never refuse worker startup --
                # the per-cycle entry logic already rejects stale quotes via
                # max_quote_age_seconds (STALE_PRICE), and exits still need to run.
                # Surface the age here instead of failing closed on it.
                healthy=True,
                market_data="healthy" if fresh else "stale_quote",
                execution="disabled",
                environment=self.client.environment.value.upper(),
                pricing_stream=self.stream_health_state().value,
                latest_price_age_seconds=age,
                message=(
                    f"Authenticated; {len(instruments)} configured instruments available; "
                    f"last price age {age:.1f}s"
                    + ("" if fresh else " (stale at startup; per-cycle entries gate on freshness)")
                ),
            )
        except OandaProviderError as exc:
            return ProviderHealth(
                market=Market.FOREX,
                provider=MarketProvider.OANDA,
                healthy=False,
                market_data="unhealthy",
                execution="disabled",
                environment=self.client.environment.value.upper(),
                pricing_stream="unhealthy",
                message=str(exc),
            )

    def stream_health_state(self, now: datetime | None = None) -> OandaStreamHealthState:
        state = self.client.stream_state
        last_message = self.client.last_stream_message_at
        if state is OandaStreamHealthState.HEALTHY and last_message is not None:
            reference = now or datetime.now(UTC)
            if (reference - last_message).total_seconds() > self.stale_seconds:
                self.client.stream_state = OandaStreamHealthState.STALE
                return OandaStreamHealthState.STALE
        return state

    async def _consume_price_stream(self, instruments: Sequence[str]) -> None:
        try:
            async for message in self.client.stream_messages(instruments):
                self._stream_message_event.set()
                if message.get("type") != "PRICE":
                    continue
                quote = normalize_oanda_price(message)
                self._latest_quotes[quote.symbol] = quote
                self.client.metrics.prices += 1
        except asyncio.CancelledError:
            raise
        except OandaAuthenticationError:
            logger.error("OANDA pricing stream authentication failed")
        except Exception as exc:  # pragma: no cover - client normally reconnects internally
            self.client.stream_state = OandaStreamHealthState.UNHEALTHY
            self.client.stream_error = type(exc).__name__
            logger.warning("OANDA pricing stream stopped: %s", type(exc).__name__)

    async def start_price_stream(self, instruments: Sequence[str]) -> None:
        symbols = tuple(
            dict.fromkeys(item.strip().upper() for item in instruments if item.strip())
        )
        if not symbols:
            raise ValueError("At least one OANDA instrument is required for pricing stream")
        if self._stream_task is not None and not self._stream_task.done():
            return
        self._stream_message_event.clear()
        self._stream_task = asyncio.create_task(
            self._consume_price_stream(symbols),
            name="oanda-practice-pricing-stream",
        )

    async def wait_for_stream_message(self, timeout_seconds: float) -> bool:
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._stream_message_event.wait()
            return True
        except TimeoutError:
            if self.client.stream_state is OandaStreamHealthState.CONNECTING:
                self.client.stream_state = OandaStreamHealthState.UNHEALTHY
                if not self.client.stream_error:
                    self.client.stream_error = "STREAM_MESSAGE_TIMEOUT"
            return False

    async def stop_price_stream(self) -> None:
        task = self._stream_task
        self._stream_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def get_candles(
        self,
        instrument: str,
        timeframe: str,
        *,
        count: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        complete_only: bool = True,
    ) -> list[CanonicalCandle]:
        return await self.client.candles(
            instrument,
            timeframe,
            count=count,
            start=start,
            end=end,
            complete_only=complete_only,
        )

    async def get_prices(self, instruments: Sequence[str]) -> dict[str, CanonicalQuote]:
        quotes = await self.client.pricing(instruments)
        self._latest_quotes.update(quotes)
        return quotes

    async def stream_prices(self, instruments: Sequence[str]) -> AsyncIterator[CanonicalQuote]:
        async for message in self.client.stream_messages(instruments):
            if message.get("type") != "PRICE":
                continue
            quote = normalize_oanda_price(message)
            self._latest_quotes[quote.symbol] = quote
            self.client.metrics.prices += 1
            yield quote

    def quote_is_stale(self, symbol: str, now: datetime | None = None) -> bool:
        quote = self._latest_quotes.get(symbol.upper())
        return quote is None or quote.age_seconds(now) > self.stale_seconds

    async def close(self) -> None:
        await self.stop_price_stream()
        await self.client.close()


@dataclass(frozen=True)
class OandaOrderIntent:
    intent_id: str
    instrument: str
    side: str
    units: float
    stop_loss: float
    take_profit: float


class OandaExecutionProvider:
    """Practice-only execution adapter; never selected by the Phase-1 runtime factory."""

    def __init__(
        self,
        client: OandaV20Client,
        *,
        configured_enabled: bool,
        trading_mode: str,
        global_kill_switch: bool,
        forex_kill_switch: bool,
    ) -> None:
        self.client = client
        self._enabled = bool(
            configured_enabled
            and client.environment is OandaEnvironment.PRACTICE
            and str(trading_mode).lower() == "paper"
            and not global_kill_switch
            and not forex_kill_switch
        )
        self._submitted_intents: set[str] = set()

    @property
    def execution_enabled(self) -> bool:
        return self._enabled

    async def account_summary(self) -> dict[str, Any]:
        return await self.client.account_summary()

    async def open_positions(self) -> list[dict[str, Any]]:
        return await self.client.open_positions()

    async def pending_orders(self) -> list[dict[str, Any]]:
        return await self.client.pending_orders()

    async def submit_order(self, intent: OandaOrderIntent) -> dict[str, Any]:
        if not self.execution_enabled:
            raise OandaExecutionDisabledError("OANDA execution is disabled (market-data/paper phase)")
        if not intent.intent_id or intent.intent_id in self._submitted_intents:
            raise OandaExecutionDisabledError("Duplicate or missing OANDA order intent ID")
        units = abs(intent.units) if intent.side.upper() == "BUY" else -abs(intent.units)
        payload = {
            "type": "MARKET",
            "instrument": intent.instrument.upper(),
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "clientExtensions": {"id": intent.intent_id[:128], "tag": "deltaquant"},
            "stopLossOnFill": {"price": str(intent.stop_loss)},
            "takeProfitOnFill": {"price": str(intent.take_profit)},
        }
        response = await self.client.create_order(payload)
        self._submitted_intents.add(intent.intent_id)
        return response

    async def close_position(self, instrument: str, units: float | None = None) -> dict[str, Any]:
        if not self.execution_enabled:
            raise OandaExecutionDisabledError("OANDA execution is disabled (market-data/paper phase)")
        payload = {"longUnits": "ALL", "shortUnits": "ALL"}
        if units is not None:
            payload = {"longUnits": str(abs(units)), "shortUnits": "NONE"}
        return await self.client.close_position(instrument, payload)
