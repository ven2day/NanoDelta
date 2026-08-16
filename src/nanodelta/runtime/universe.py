"""Durable publication of the explicitly configured runtime universe."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from nanodelta.contracts import Market, Provider, utc
from nanodelta.persistence.migrations import Connection


@dataclass(frozen=True)
class ConfiguredInstrument:
    market: Market
    symbol: str
    provider: Provider
    provider_symbol: str
    timeframes: tuple[str, ...]
    trade_horizon: str

    def __post_init__(self) -> None:
        if not all((self.symbol, self.provider_symbol, self.timeframes, self.trade_horizon)):
            raise ValueError("configured instrument fields cannot be empty")


def publish_configured_universe(
    connect: Callable[[], Connection],
    market: Market,
    instruments: Sequence[ConfiguredInstrument],
    *,
    configured_at: datetime,
) -> None:
    """Replace one market's enabled set while retaining disabled audit rows."""
    configured_at = utc(configured_at, "configured_at")
    if any(instrument.market is not market for instrument in instruments):
        raise ValueError("configured universe cannot mix markets")
    if len({instrument.symbol for instrument in instruments}) != len(instruments):
        raise ValueError("configured universe symbols must be unique")
    connection = connect()
    try:
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE control.market_universe SET enabled=false,configured_at=%s WHERE market=%s",
            (configured_at, market.value),
        )
        for instrument in instruments:
            cursor.execute(
                "INSERT INTO control.market_universe "
                "(market,symbol,provider,provider_symbol,timeframes,trade_horizon,enabled,"
                "configured_at) VALUES (%s,%s,%s,%s,%s::jsonb,%s,true,%s) "
                "ON CONFLICT (market,symbol) DO UPDATE SET provider=EXCLUDED.provider,"
                "provider_symbol=EXCLUDED.provider_symbol,timeframes=EXCLUDED.timeframes,"
                "trade_horizon=EXCLUDED.trade_horizon,enabled=true,"
                "configured_at=EXCLUDED.configured_at",
                (
                    instrument.market.value,
                    instrument.symbol,
                    instrument.provider.value,
                    instrument.provider_symbol,
                    json.dumps(instrument.timeframes, separators=(",", ":")),
                    instrument.trade_horizon,
                    configured_at,
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
