"""Versioned Feature/Gold records built from settled canonical candles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import pandas as pd

from src.core.models import Market

if TYPE_CHECKING:
    from src.core.aggregation.consolidation import FeatureSnapshot


@dataclass(frozen=True)
class FeatureRecord:
    """Serializable Gold-layer materialization of one existing feature snapshot."""

    snapshot_id: str
    market: Market
    provider: str
    symbol: str
    timeframe: str
    settled_candle_timestamp: datetime
    feature_version: str
    volume_type: str
    indicators: Mapping[str, Any]
    market_relative: Mapping[str, Any] | None

    @classmethod
    def from_snapshot(cls, snapshot: FeatureSnapshot) -> FeatureRecord:
        timestamp = pd.Timestamp(snapshot.settled_candle_timestamp)
        if timestamp.tzinfo is None:
            raise ValueError("Feature snapshot timestamp must be timezone-aware")
        if not snapshot.snapshot_id.strip():
            raise ValueError("Feature snapshot_id cannot be empty")
        if not snapshot.feature_version.strip():
            raise ValueError("Feature version cannot be empty")
        indicators = snapshot.indicators.to_dict()
        relative = (
            snapshot.market_relative.to_dict() if snapshot.market_relative is not None else None
        )
        return cls(
            snapshot_id=snapshot.snapshot_id,
            market=Market.parse(snapshot.market),
            provider=snapshot.provider.strip().upper(),
            symbol=snapshot.symbol.strip().upper(),
            timeframe=snapshot.timeframe.value,
            settled_candle_timestamp=timestamp.tz_convert("UTC").to_pydatetime(),
            feature_version=snapshot.feature_version,
            volume_type=snapshot.volume_type.strip().upper(),
            indicators=MappingProxyType(indicators),
            market_relative=MappingProxyType(relative) if relative is not None else None,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "market": self.market.value,
            "provider": self.provider,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "settled_candle_timestamp": self.settled_candle_timestamp.astimezone(UTC).isoformat(),
            "feature_version": self.feature_version,
            "volume_type": self.volume_type,
            "indicators": dict(self.indicators),
            "market_relative": (
                dict(self.market_relative) if self.market_relative is not None else None
            ),
        }
