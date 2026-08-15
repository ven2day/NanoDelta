"""Schema and repository boundaries shared by independent market domains."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.config.secrets import redact_secrets
from src.core.models import Market
from src.core.namespaces import normalize_market

_TABLES = {
    "market_candles",
    "signals",
    "decisions",
    "positions",
    "orders",
    "strategy_registry",
    "ml_predictions",
}

# DDL for the trading tables this repository owns, derived directly from the
# INSERT/SELECT statements below (column names, JSONB payloads, and each
# ON CONFLICT target's uniqueness). ``market_candles`` is created by CandleStore
# and ``model_registry`` by ModelRegistry — both already self-create their own
# schema objects; the trading repository must do the same so a *fresh* database
# is writable without a manual step (this repo ships no migration tooling). Every
# statement is idempotent, so an already-provisioned database is left untouched.
_TRADING_TABLE_DDL: tuple[tuple[str, str], ...] = (
    (
        "signals",
        "signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, timeframe TEXT NOT NULL, "
        "side TEXT NOT NULL, strategy TEXT NOT NULL, payload JSONB NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL",
    ),
    (
        "decisions",
        "decision_id TEXT PRIMARY KEY, signal_id TEXT, symbol TEXT NOT NULL, "
        "final_action TEXT NOT NULL, rejection_reason TEXT, payload JSONB NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL",
    ),
    (
        "positions",
        "position_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL, "
        "quantity DOUBLE PRECISION NOT NULL, payload JSONB NOT NULL, "
        "updated_at TIMESTAMPTZ NOT NULL",
    ),
    (
        # persist_order/persist_paper_fill upsert ON CONFLICT (intent_id), so
        # intent_id — not order_id — carries the uniqueness constraint.
        "orders",
        "intent_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, symbol TEXT NOT NULL, "
        "side TEXT NOT NULL, status TEXT NOT NULL, payload JSONB NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL",
    ),
    (
        "strategy_registry",
        "identity TEXT PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL",
    ),
    (
        "ml_predictions",
        "prediction_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, timeframe TEXT NOT NULL, "
        "settled_candle_timestamp TIMESTAMPTZ NOT NULL, model_version TEXT NOT NULL, "
        "payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL",
    ),
)


class MarketRepositoryBoundaryError(ValueError):
    """Raised when code attempts to cross a market-owned persistence boundary."""


@dataclass(frozen=True)
class MarketRepositoryBoundary:
    market: Market | str

    @property
    def schema(self) -> str:
        return normalize_market(self.market).value.lower()

    def qualify(self, table: str) -> str:
        normalized = table.strip().lower()
        if normalized not in _TABLES:
            raise MarketRepositoryBoundaryError(f"Table is not market-owned: {table!r}")
        return f'"{self.schema}"."{normalized}"'

    def require_market(self, value: Market | str) -> None:
        if normalize_market(value) is not normalize_market(self.market):
            raise MarketRepositoryBoundaryError(
                f"{self.schema} repository cannot access {normalize_market(value).value.lower()} data"
            )


class SchemaBoundCandleRepository:
    """Pins the legacy CandleStore API to exactly one market/provider.

    The delegate remains usable during the non-destructive schema migration. New
    market workers receive this wrapper and therefore cannot pass another market.
    """

    def __init__(
        self,
        delegate: Any,
        *,
        market: Market | str,
        provider: str,
        analysis_loader: Callable[..., pd.DataFrame] | None = None,
    ) -> None:
        self._delegate = delegate
        self.boundary = MarketRepositoryBoundary(market)
        self.market = normalize_market(market).value
        self.provider = provider.strip().upper()
        self._analysis_loader = analysis_loader

    @property
    def timescale_enabled(self) -> bool:
        return bool(getattr(self._delegate, "timescale_enabled", False))

    def upsert_frame(
        self,
        symbol: str,
        timeframe: str,
        frame: pd.DataFrame,
        *,
        source: str,
        market: str | None = None,
        provider: str | None = None,
        volume_type: str = "UNKNOWN",
    ) -> int:
        if market is not None:
            self.boundary.require_market(market)
        if provider is not None and provider.strip().upper() != self.provider:
            raise MarketRepositoryBoundaryError(
                f"{self.market} repository cannot write provider {provider!r}"
            )
        return int(
            self._delegate.upsert_frame(
                symbol,
                timeframe,
                frame,
                source=source,
                market=self.market,
                provider=self.provider,
                volume_type=volume_type,
            )
        )

    def load_frame(self, symbol: str, timeframe: str, **kwargs: Any) -> pd.DataFrame:
        kwargs.pop("market", None)
        kwargs.pop("provider", None)
        result = self._delegate.load_frame(
            symbol, timeframe, market=self.market, provider=self.provider, **kwargs
        )
        if result is None:
            return pd.DataFrame()
        if not isinstance(result, pd.DataFrame):
            raise TypeError("Candle repository returned a non-DataFrame result")
        return result

    def load_analysis_frame(self, symbol: str, timeframe: str, **kwargs: Any) -> pd.DataFrame:
        kwargs.pop("market", None)
        kwargs.pop("provider", None)
        if self._analysis_loader is not None:
            result = self._analysis_loader(self._delegate, symbol, timeframe, **kwargs)
        else:
            result = self._delegate.load_analysis_frame(
                symbol, timeframe, market=self.market, provider=self.provider, **kwargs
            )
        if not isinstance(result, pd.DataFrame):
            raise TypeError("Candle repository returned a non-DataFrame analysis result")
        return result

    def coverage(self, symbol: str, timeframe: str, **kwargs: Any) -> Any:
        kwargs.pop("market", None)
        kwargs.pop("provider", None)
        return self._delegate.coverage(
            symbol, timeframe, market=self.market, provider=self.provider, **kwargs
        )


class SchemaBoundTradingRepository:
    """PostgreSQL JSON repositories pinned to one market-owned schema.

    This is deliberately a persistence boundary, not a trading service.  Callers
    provide already-produced signals/decisions and this class can only write the
    schema derived from its market.  Payloads are redacted before serialization so
    broker credentials cannot leak into the decision journal.
    """

    def __init__(self, engine: Engine, *, market: Market | str, provider: str) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("Market trading persistence requires PostgreSQL")
        self.engine = engine
        self.boundary = MarketRepositoryBoundary(market)
        self.market = normalize_market(market).value
        self.provider = provider.strip().upper()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create this market's schema and trading tables if they are absent.

        A fresh database has no migration tool to lean on, and the eligibility
        seed on the very first cycle writes to ``<schema>.strategy_registry``
        before any other code touches it — so without this the worker crashes on
        an UndefinedTable. All DDL is ``IF NOT EXISTS`` and runs once at
        construction, leaving an already-provisioned database unchanged.
        """
        schema = self.boundary.schema
        with self.engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
            for table, columns in _TRADING_TABLE_DDL:
                connection.execute(
                    text(f'CREATE TABLE IF NOT EXISTS "{schema}"."{table}" ({columns})')
                )

    @staticmethod
    def _json(payload: dict[str, Any]) -> str:
        return json.dumps(
            redact_secrets(payload),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )

    @staticmethod
    def _when(value: datetime | str | None) -> datetime:
        if value is None:
            return datetime.now(UTC)
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        parsed = pd.Timestamp(value)
        if parsed.tzinfo is None:
            parsed = parsed.tz_localize("UTC")
        return parsed.tz_convert("UTC").to_pydatetime()

    def persist_signal(
        self,
        *,
        signal_id: str,
        symbol: str,
        timeframe: str,
        side: str,
        strategy: str,
        payload: dict[str, Any],
        created_at: datetime | str | None = None,
    ) -> None:
        table = self.boundary.qualify("signals")
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {table} "
                    "(signal_id, symbol, timeframe, side, strategy, payload, created_at) "
                    "VALUES (:signal_id, :symbol, :timeframe, :side, :strategy, "
                    "CAST(:payload AS jsonb), :created_at) "
                    "ON CONFLICT (signal_id) DO UPDATE SET payload=EXCLUDED.payload"
                ),
                {
                    "signal_id": signal_id,
                    "symbol": symbol.strip().upper(),
                    "timeframe": timeframe.strip().lower(),
                    "side": side.strip().upper(),
                    "strategy": strategy.strip().lower(),
                    "payload": self._json(payload),
                    "created_at": self._when(created_at),
                },
            )

    def persist_decision(
        self,
        *,
        decision_id: str,
        signal_id: str | None,
        symbol: str,
        final_action: str,
        rejection_reason: str | None,
        payload: dict[str, Any],
        created_at: datetime | str | None = None,
    ) -> None:
        table = self.boundary.qualify("decisions")
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {table} "
                    "(decision_id, signal_id, symbol, final_action, rejection_reason, payload, created_at) "
                    "VALUES (:decision_id, :signal_id, :symbol, :final_action, "
                    ":rejection_reason, CAST(:payload AS jsonb), :created_at) "
                    "ON CONFLICT (decision_id) DO UPDATE SET "
                    "final_action=EXCLUDED.final_action, rejection_reason=EXCLUDED.rejection_reason, "
                    "payload=EXCLUDED.payload"
                ),
                {
                    "decision_id": decision_id,
                    "signal_id": signal_id,
                    "symbol": symbol.strip().upper(),
                    "final_action": final_action.strip().upper(),
                    "rejection_reason": rejection_reason,
                    "payload": self._json(payload),
                    "created_at": self._when(created_at),
                },
            )

    def persist_position(
        self,
        *,
        position_id: str,
        symbol: str,
        side: str,
        quantity: float,
        payload: dict[str, Any],
        updated_at: datetime | str | None = None,
    ) -> None:
        table = self.boundary.qualify("positions")
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {table} "
                    "(position_id, symbol, side, quantity, payload, updated_at) "
                    "VALUES (:position_id, :symbol, :side, :quantity, CAST(:payload AS jsonb), "
                    ":updated_at) ON CONFLICT (position_id) DO UPDATE SET "
                    "side=EXCLUDED.side, quantity=EXCLUDED.quantity, "
                    "payload=EXCLUDED.payload, updated_at=EXCLUDED.updated_at"
                ),
                {
                    "position_id": position_id,
                    "symbol": symbol.strip().upper(),
                    "side": side.strip().upper(),
                    "quantity": float(quantity),
                    "payload": self._json(payload),
                    "updated_at": self._when(updated_at),
                },
            )

    def load_open_positions(self) -> list[dict[str, Any]]:
        """Return market-local open paper positions; never crosses schemas."""

        table = self.boundary.qualify("positions")
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"SELECT payload, quantity FROM {table} "
                    "WHERE COALESCE(payload->>'status', 'OPEN') = 'OPEN' "
                    "ORDER BY updated_at"
                )
            )
            output: list[dict[str, Any]] = []
            for row in rows:
                payload = row[0]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if isinstance(payload, dict):
                    item = dict(payload)
                    # Backfill the canonical quantity from its relational column for
                    # positions written before quantity was included in the JSON payload.
                    item.setdefault("quantity", float(row[1] or 0.0))
                    output.append(item)
            return output

    def load_final_decisions(
        self, *, days: int = 7, limit: int = 250
    ) -> list[dict[str, Any]]:
        """Load durable paper BUY/SELL decisions, never diagnostic candidates."""

        table = self.boundary.qualify("decisions")
        cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"SELECT decision_id, signal_id, symbol, final_action, "
                    f"rejection_reason, payload, created_at FROM {table} "
                    "WHERE final_action IN ('PAPER_BUY', 'PAPER_SELL') "
                    "AND created_at >= :cutoff ORDER BY created_at DESC LIMIT :limit"
                ),
                {"cutoff": cutoff, "limit": max(1, min(limit, 1_000))},
            )
            output: list[dict[str, Any]] = []
            for row in rows:
                payload = row[5]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                item = dict(payload) if isinstance(payload, dict) else {}
                item.update(
                    {
                        "decision_id": str(row[0]),
                        "signal_id": row[1],
                        "market": self.market,
                        "symbol": str(row[2]).upper(),
                        "instrument": str(row[2]).upper(),
                        "final_action": str(row[3]).upper(),
                        "rejection_reason": row[4],
                        "created_at": self._when(row[6]).isoformat(),
                    }
                )
                item.setdefault("side", str(row[3]).removeprefix("PAPER_"))
                candidate = item.get("candidate")
                if isinstance(candidate, dict):
                    for key in (
                        "timeframe",
                        "strategy",
                        "supporting_strategies",
                        "settled_candle_timestamp",
                        "entry_price",
                        "stop_loss",
                        "target_price",
                    ):
                        if item.get(key) is None and candidate.get(key) is not None:
                            item[key] = candidate[key]
                item.setdefault("current_stage", "FINAL_PAPER_DECISION")
                item.setdefault("decision_status", item["current_stage"])
                output.append(item)
            return output

    def load_decision(self, decision_id: str) -> dict[str, Any] | None:
        """Return one market-local decision payload for lifecycle reconciliation."""

        table = self.boundary.qualify("decisions")
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT signal_id, symbol, final_action, rejection_reason, payload, "
                    f"created_at FROM {table} WHERE decision_id = :decision_id"
                ),
                {"decision_id": decision_id},
            ).first()
        if row is None:
            return None
        payload = row[4]
        if isinstance(payload, str):
            payload = json.loads(payload)
        item = dict(payload) if isinstance(payload, dict) else {}
        item.update(
            {
                "decision_id": decision_id,
                "signal_id": row[0],
                "market": self.market,
                "symbol": str(row[1]).upper(),
                "instrument": str(row[1]).upper(),
                "final_action": str(row[2]).upper(),
                "rejection_reason": row[3],
                "created_at": self._when(row[5]).isoformat(),
            }
        )
        return item

    def persist_paper_fill(
        self,
        *,
        intent_id: str,
        order_id: str,
        position_id: str,
        symbol: str,
        side: str,
        quantity: float,
        order_payload: dict[str, Any],
        position_payload: dict[str, Any],
        created_at: datetime | str | None = None,
    ) -> bool:
        """Atomically create one local-paper fill and reject duplicate intents."""

        orders = self.boundary.qualify("orders")
        positions = self.boundary.qualify("positions")
        when = self._when(created_at)
        with self.engine.begin() as connection:
            inserted = connection.execute(
                text(
                    f"INSERT INTO {orders} "
                    "(order_id, intent_id, symbol, side, status, payload, created_at) "
                    "VALUES (:order_id, :intent_id, :symbol, :side, 'SIMULATED_FILLED', "
                    "CAST(:payload AS jsonb), :created_at) "
                    "ON CONFLICT (intent_id) DO NOTHING RETURNING intent_id"
                ),
                {
                    "order_id": order_id,
                    "intent_id": intent_id,
                    "symbol": symbol.strip().upper(),
                    "side": side.strip().upper(),
                    "payload": self._json(order_payload),
                    "created_at": when,
                },
            ).first()
            if inserted is None:
                return False
            connection.execute(
                text(
                    f"INSERT INTO {positions} "
                    "(position_id, symbol, side, quantity, payload, updated_at) "
                    "VALUES (:position_id, :symbol, :side, :quantity, "
                    "CAST(:payload AS jsonb), :updated_at) "
                    "ON CONFLICT (position_id) DO UPDATE SET "
                    "side=EXCLUDED.side, quantity=EXCLUDED.quantity, "
                    "payload=EXCLUDED.payload, updated_at=EXCLUDED.updated_at"
                ),
                {
                    "position_id": position_id,
                    "symbol": symbol.strip().upper(),
                    "side": side.strip().upper(),
                    "quantity": float(quantity),
                    "payload": self._json(position_payload),
                    "updated_at": when,
                },
            )
            return True

    def persist_paper_close(
        self,
        *,
        intent_id: str,
        order_id: str,
        position_id: str,
        decision_id: str,
        symbol: str,
        side: str,
        order_payload: dict[str, Any],
        position_payload: dict[str, Any],
        decision_payload: dict[str, Any],
        closed_at: datetime | str | None = None,
    ) -> bool:
        """Atomically persist a local-paper exit and close its position/decision."""

        orders = self.boundary.qualify("orders")
        positions = self.boundary.qualify("positions")
        decisions = self.boundary.qualify("decisions")
        when = self._when(closed_at)
        with self.engine.begin() as connection:
            inserted = connection.execute(
                text(
                    f"INSERT INTO {orders} "
                    "(order_id, intent_id, symbol, side, status, payload, created_at) "
                    "VALUES (:order_id, :intent_id, :symbol, :side, 'SIMULATED_FILLED', "
                    "CAST(:payload AS jsonb), :created_at) "
                    "ON CONFLICT (intent_id) DO NOTHING RETURNING intent_id"
                ),
                {
                    "order_id": order_id,
                    "intent_id": intent_id,
                    "symbol": symbol.strip().upper(),
                    "side": side.strip().upper(),
                    "payload": self._json(order_payload),
                    "created_at": when,
                },
            ).first()
            if inserted is None:
                return False
            position_result = connection.execute(
                text(
                    f"UPDATE {positions} SET quantity = 0, payload = CAST(:payload AS jsonb), "
                    "updated_at = :updated_at WHERE position_id = :position_id"
                ),
                {
                    "position_id": position_id,
                    "payload": self._json(position_payload),
                    "updated_at": when,
                },
            )
            if getattr(position_result, "rowcount", 1) != 1:
                raise RuntimeError("PAPER_POSITION_NOT_FOUND")
            connection.execute(
                text(
                    f"UPDATE {decisions} SET rejection_reason = NULL, "
                    "payload = CAST(:payload AS jsonb) WHERE decision_id = :decision_id"
                ),
                {
                    "decision_id": decision_id,
                    "payload": self._json(decision_payload),
                },
            )
            return True

    def persist_order(
        self,
        *,
        order_id: str,
        intent_id: str,
        symbol: str,
        side: str,
        status: str,
        payload: dict[str, Any],
        created_at: datetime | str | None = None,
    ) -> None:
        table = self.boundary.qualify("orders")
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {table} "
                    "(order_id, intent_id, symbol, side, status, payload, created_at) "
                    "VALUES (:order_id, :intent_id, :symbol, :side, :status, "
                    "CAST(:payload AS jsonb), :created_at) "
                    "ON CONFLICT (intent_id) DO UPDATE SET "
                    "status=EXCLUDED.status, payload=EXCLUDED.payload"
                ),
                {
                    "order_id": order_id,
                    "intent_id": intent_id,
                    "symbol": symbol.strip().upper(),
                    "side": side.strip().upper(),
                    "status": status.strip().upper(),
                    "payload": self._json(payload),
                    "created_at": self._when(created_at),
                },
            )

    def persist_strategy_eligibility(
        self,
        *,
        identity: str,
        payload: dict[str, Any],
        updated_at: datetime | str | None = None,
    ) -> None:
        table = self.boundary.qualify("strategy_registry")
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {table} (identity, payload, updated_at) "
                    "VALUES (:identity, CAST(:payload AS jsonb), :updated_at) "
                    "ON CONFLICT (identity) DO UPDATE SET "
                    "payload=EXCLUDED.payload, updated_at=EXCLUDED.updated_at"
                ),
                {
                    "identity": identity,
                    "payload": self._json(payload),
                    "updated_at": self._when(updated_at),
                },
            )

    def persist_ml_prediction(
        self,
        *,
        prediction_id: str,
        symbol: str,
        timeframe: str,
        settled_candle_timestamp: datetime | str,
        model_version: str,
        payload: dict[str, Any],
        created_at: datetime | str | None = None,
    ) -> None:
        table = self.boundary.qualify("ml_predictions")
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {table} "
                    "(prediction_id, symbol, timeframe, settled_candle_timestamp, "
                    "model_version, payload, created_at) VALUES "
                    "(:prediction_id, :symbol, :timeframe, :settled, :model_version, "
                    "CAST(:payload AS jsonb), :created_at) "
                    "ON CONFLICT (prediction_id) DO UPDATE SET payload=EXCLUDED.payload"
                ),
                {
                    "prediction_id": prediction_id,
                    "symbol": symbol.strip().upper(),
                    "timeframe": timeframe.strip().lower(),
                    "settled": self._when(settled_candle_timestamp),
                    "model_version": model_version,
                    "payload": self._json(payload),
                    "created_at": self._when(created_at),
                },
            )
