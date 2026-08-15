"""Thin orchestration across Bronze, Silver, and Gold."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from nanodelta.contracts import (
    CanonicalCandle,
    EventType,
    FeatureRecord,
    Market,
    Provider,
    RawRecord,
)
from nanodelta.features import materialize_features
from nanodelta.markets import adapter_for
from nanodelta.storage import FileLake


@dataclass(frozen=True)
class IngestionResult:
    raw: RawRecord
    canonical: CanonicalCandle | None
    bronze_created: bool
    silver_created: bool
    rejection_reason: str | None = None


class EtlPipeline:
    def __init__(self, lake: FileLake) -> None:
        self.lake = lake

    def ingest(
        self,
        *,
        market: Market,
        provider: Provider,
        event_type: EventType,
        provider_symbol: str,
        payload: dict[str, object],
        received_at: datetime,
    ) -> IngestionResult:
        raw = RawRecord.create(
            market=market,
            provider=provider,
            event_type=event_type,
            provider_symbol=provider_symbol,
            received_at=received_at,
            payload=payload,
        )
        bronze_created = self.lake.write(
            market=market,
            layer="bronze",
            event_time=raw.received_at,
            record_id=raw.record_id,
            record=raw.to_dict(),
        )
        if event_type is not EventType.CANDLE:
            return IngestionResult(raw, None, bronze_created, False)
        try:
            canonical = adapter_for(market, provider)(raw)
        except (KeyError, TypeError, ValueError) as exc:
            return IngestionResult(raw, None, bronze_created, False, str(exc))
        if not canonical.is_settled:
            return IngestionResult(raw, canonical, bronze_created, False, "candle is not settled")
        silver_created = self.lake.write(
            market=market,
            layer="silver",
            event_time=canonical.open_time,
            record_id=canonical.record_id,
            record=canonical.to_dict(),
        )
        return IngestionResult(raw, canonical, bronze_created, silver_created)

    def build_gold(self, candles: list[CanonicalCandle]) -> list[FeatureRecord]:
        features = materialize_features(candles)
        for feature in features:
            self.lake.write(
                market=feature.market,
                layer="gold",
                event_time=feature.event_time,
                record_id=feature.record_id,
                record=feature.to_dict(),
            )
        return features
