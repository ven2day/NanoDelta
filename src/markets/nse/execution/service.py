"""
Unified execution service.

One mode-switched entry point for placing orders, so the live loop has a single code path
instead of calling the paper engine (or a broker) directly. Adds the safety the audit found
missing:

- **Order idempotency** — every submit carries an idempotency key; a repeat (retry, restart,
  duplicate approved-trade) returns the prior result instead of placing a second order.
- **Shadow mode** — mirrors exactly what a live run would do (sizing + the order it *would*
  send), simulating the fill against the paper engine, but never contacts a broker. This is
  the safe bridge to live trading.
- **No silent downgrade** — a `live` request without `allow_live_orders`, without
  `trading_mode=live`, or without broker credentials resolves to SHADOW with a loud warning,
  rather than quietly trading on the local paper wallet as if it were real. `dhan_paper` is
  simulate-only: it NEVER reaches a live route, for any combination of flags (see C-2 in
  docs/audits/DeltaQuant-Quant-Risk-Review.md and `resolve_effective_execution_mode` in
  src/config/settings.py) — there is no verified Dhan sandbox endpoint, so "paper" in its name
  cannot be backed by an actual sandbox, and pretending otherwise was exactly the hazard.

Real broker order submission IS wired here: when a `broker_executor` (`LiveBrokerExecutor`)
is attached, `submit_async` calls `_submit_live`, which places and confirms a genuine DhanHQ
order. The safety boundary is entirely in mode resolution above, not in this file being a
stub — only `live`, with `trading_mode=live` AND `allow_live_orders=true` AND valid Dhan
credentials, ever sends a real order.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import Column, DateTime, String, Text
from sqlalchemy.orm import Session

from src.config import get_settings
from src.config.settings import resolve_effective_execution_mode
from src.db.base import Base, get_session
from src.markets.nse.broker.dhan.execution import OrderRequest, OrderSide, OrderStatus, OrderType
from src.markets.nse.execution.live_executor import LiveBrokerExecutor
from src.markets.nse.execution.paper_engine import LocalPaperEngine

logger = logging.getLogger(__name__)

MAX_IDEMPOTENCY_ENTRIES = 5000


class ExecutionMode(str, Enum):
    LOCAL_PAPER = "local_paper"
    SHADOW = "shadow"
    DHAN_PAPER = "dhan_paper"
    LIVE = "live"


@dataclass
class ExecutionResult:
    """Outcome of an execution submission."""

    status: str  # FILLED | REJECTED | BLOCKED | DUPLICATE
    symbol: str
    side: str
    quantity: int
    fill_price: float
    mode: str
    client_order_id: str
    order_id: str = ""
    is_shadow: bool = False
    is_duplicate: bool = False
    message: str = ""
    trade_id: str = ""
    position_id: str = ""
    realized_pnl: float = 0.0
    entry_charges: float = 0.0
    exit_charges: float = 0.0

    @property
    def filled(self) -> bool:
        return self.status == "FILLED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "fill_price": self.fill_price,
            "mode": self.mode,
            "client_order_id": self.client_order_id,
            "order_id": self.order_id,
            "is_shadow": self.is_shadow,
            "is_duplicate": self.is_duplicate,
            "message": self.message,
            "trade_id": self.trade_id,
            "position_id": self.position_id,
            "realized_pnl": self.realized_pnl,
            "entry_charges": self.entry_charges,
            "exit_charges": self.exit_charges,
        }


class IdempotencyRecord(Base):
    """One row per idempotency key ever recorded."""

    __tablename__ = "idempotency_keys"

    key = Column(String(200), primary_key=True)
    namespace = Column(String(40), default="paper_market_data", nullable=False, index=True)
    result_json = Column(Text, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
        index=True,
    )


class IdempotencyStore:
    """
    Records submitted order keys so the same intent isn't placed twice.

    Persisted to Postgres so a restart mid-run does not replay orders. Bounded to the
    most recent MAX_IDEMPOTENCY_ENTRIES keys.
    """

    def __init__(
        self,
        database_url: str | None = None,
        namespace: str = "paper_market_data",
    ) -> None:
        self._database_url = database_url
        self.namespace = namespace

    def _session(self) -> Session:
        return get_session(self._database_url)

    def _storage_key(self, key: str) -> str:
        # ``key`` is the legacy primary key, so it cannot be unique per namespace at
        # the schema level without a destructive primary-key migration. Prefixing the
        # stored value makes identical caller keys independently durable in both modes.
        return f"{self.namespace}\x1f{key}"

    def seen(self, key: str) -> dict[str, Any] | None:
        session = self._session()
        try:
            row = (
                session.query(IdempotencyRecord)
                .filter_by(key=self._storage_key(key), namespace=self.namespace)
                .first()
            )
            return json.loads(str(row.result_json)) if row is not None else None
        except Exception:
            logger.exception("Failed to check idempotency store")
            return None
        finally:
            session.close()

    def record(self, key: str, result: dict[str, Any]) -> None:
        session = self._session()
        try:
            row = (
                session.query(IdempotencyRecord)
                .filter_by(key=self._storage_key(key), namespace=self.namespace)
                .first()
            )
            now = datetime.now(UTC)
            if row is None:
                session.add(
                    IdempotencyRecord(
                        key=self._storage_key(key),
                        namespace=self.namespace,
                        result_json=json.dumps(result),
                        created_at=now,
                    )
                )
            else:
                row.result_json = json.dumps(result)  # type: ignore[assignment]
                row.created_at = now  # type: ignore[assignment]
            session.commit()

            count = session.query(IdempotencyRecord).filter_by(namespace=self.namespace).count()
            if count > MAX_IDEMPOTENCY_ENTRIES:
                excess = count - MAX_IDEMPOTENCY_ENTRIES
                oldest = (
                    session.query(IdempotencyRecord)
                    .filter_by(namespace=self.namespace)
                    .order_by(IdempotencyRecord.created_at)
                    .limit(excess)
                    .all()
                )
                for old in oldest:
                    session.delete(old)
                session.commit()
        except Exception:
            logger.exception("Failed to persist idempotency record")
            session.rollback()
        finally:
            session.close()


@dataclass
class ExecutionService:
    """Mode-switched order submission with idempotency and a shadow (dry-run) mode."""

    engine: LocalPaperEngine
    mode: ExecutionMode = ExecutionMode.LOCAL_PAPER
    allow_live_orders: bool = False
    idempotency: IdempotencyStore = field(default_factory=IdempotencyStore)
    kill_switch: Callable[[], bool] | None = None
    broker_executor: LiveBrokerExecutor | None = None
    execution_repository: Any | None = None
    _effective_mode: ExecutionMode = field(init=False)

    def __post_init__(self) -> None:
        self._effective_mode = self._resolve_mode()
        logger.info(
            "ExecutionService ready | requested_mode=%s effective_mode=%s "
            "allow_live_orders=%s REAL_ORDERS=%s",
            self.mode.value,
            self._effective_mode.value,
            self.allow_live_orders,
            "YES" if self.real_orders_active else "NO",
        )

    @classmethod
    def from_settings(
        cls,
        engine: LocalPaperEngine,
        idempotency: IdempotencyStore | None = None,
        kill_switch: Callable[[], bool] | None = None,
    ) -> ExecutionService:
        # DeltaQuant's runnable product is permanently paper-only. Keep the enum and
        # lower-level broker test doubles for backwards-compatible isolated tests, but
        # configuration can never arm them through the application factory.
        get_settings()
        return cls(
            engine=engine,
            mode=ExecutionMode.LOCAL_PAPER,
            allow_live_orders=False,
            idempotency=(
                idempotency
                if idempotency is not None
                else IdempotencyStore(namespace=engine.namespace)
            ),
            kill_switch=kill_switch,
        )

    @property
    def effective_mode(self) -> ExecutionMode:
        return self._effective_mode

    @property
    def real_orders_active(self) -> bool:
        """True only when this service can actually reach a real broker order.

        Requires the effective mode to be LIVE (which, via ``_resolve_mode``, already
        implies trading_mode=live AND allow_live_orders AND valid Dhan credentials —
        see ``resolve_effective_execution_mode``) *and* a broker executor attached.
        ``local_paper``, ``shadow``, and ``dhan_paper`` (which always resolves to
        shadow) can never make this True.
        """
        return self._effective_mode == ExecutionMode.LIVE and self.broker_executor is not None

    def _resolve_mode(self) -> ExecutionMode:
        """Resolve the mode actually used, never silently downgrading a live request.

        Delegates to ``resolve_effective_execution_mode`` (src/config/settings.py) for
        the actual conjunction so the invariant lives in exactly one place; this method
        only adds the specific, actionable log message for *why* a downgrade happened.
        """
        settings = get_settings()
        trading_mode = getattr(settings, "trading_mode", "paper")
        has_dhan_credentials = bool(
            getattr(settings, "dhan_client_id", None)
            and getattr(settings, "dhan_access_token", None)
        )
        resolved_value = resolve_effective_execution_mode(
            self.mode.value,
            allow_live_orders=self.allow_live_orders,
            trading_mode=trading_mode,
            has_dhan_credentials=has_dhan_credentials,
        )
        resolved = ExecutionMode(resolved_value)

        if resolved is not self.mode:
            if self.mode is ExecutionMode.DHAN_PAPER:
                logger.warning(
                    "execution_mode=dhan_paper never reaches a live broker route (it "
                    "simulates against Dhan-shaped data only — see C-2) — running in SHADOW."
                )
            elif not self.allow_live_orders:
                logger.warning(
                    "execution_mode=%s but allow_live_orders is False — running in SHADOW "
                    "(no real orders sent).",
                    self.mode.value,
                )
            elif trading_mode != "live":
                logger.error(
                    "execution_mode=%s but trading_mode=%r (not 'live') — running in SHADOW. "
                    "This is the exact C-2 hazard: a paper-labeled trading_mode with the "
                    "live-order gate armed. Set TRADING_MODE=live to confirm real intent.",
                    self.mode.value,
                    trading_mode,
                )
            elif not has_dhan_credentials:
                logger.error(
                    "execution_mode=%s requested but Dhan credentials are missing — running "
                    "in SHADOW, NOT downgrading silently to local paper.",
                    self.mode.value,
                )
        return resolved

    @staticmethod
    def _client_order_id(idempotency_key: str) -> str:
        # Deterministic client order id derived from the idempotency key, so a broker (later)
        # can also dedupe on it.
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16].upper()
        return f"PAPER-{digest}"

    def _guard(
        self, symbol: str, side: str, quantity: int, idempotency_key: str, client_order_id: str
    ) -> ExecutionResult | None:
        """Shared pre-submission guards: idempotency dedup + kill switch."""
        prior = self.idempotency.seen(idempotency_key)
        if prior is not None:
            logger.warning("Duplicate submission suppressed for key=%s", idempotency_key)
            return ExecutionResult(
                status="DUPLICATE",
                symbol=symbol,
                side=side,
                quantity=quantity,
                fill_price=float(prior.get("fill_price", 0.0)),
                mode=self._effective_mode.value,
                client_order_id=client_order_id,
                order_id=str(prior.get("order_id", "")),
                is_shadow=bool(prior.get("is_shadow", False)),
                is_duplicate=True,
                message="idempotency hit — order already submitted",
            )
        if self.kill_switch is not None and self.kill_switch():
            logger.warning("Kill switch active — blocking %s %s %s", side, quantity, symbol)
            return ExecutionResult(
                status="BLOCKED",
                symbol=symbol,
                side=side,
                quantity=quantity,
                fill_price=0.0,
                mode=self._effective_mode.value,
                client_order_id=client_order_id,
                message="kill switch active",
            )
        return None

    def _fill_paper(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        order_type: str,
        client_order_id: str,
        idempotency_key: str,
        trade_id: str,
        position_id: str,
        stop_loss: float,
        target_price: float,
        strategy: str,
        reason: str,
        entry_data_source: str,
    ) -> ExecutionResult:
        """Simulate a fill against the paper engine (local_paper and shadow)."""
        is_shadow = self._effective_mode == ExecutionMode.SHADOW
        order = self.engine.place_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            current_price=price,
            order_type=order_type,
            trade_id=trade_id,
            position_id=position_id,
            idempotency_key=idempotency_key,
            stop_loss=stop_loss,
            target_price=target_price,
            strategy=strategy,
            reason=reason,
            entry_data_source=entry_data_source,
        )
        message = "SHADOW: simulated, not sent to broker" if is_shadow else "local paper fill"
        return ExecutionResult(
            status=order.status,
            symbol=symbol,
            side=side,
            # order.quantity is what the engine actually filled, not the requested
            # amount — matters for PARTIALLY_FILLED, where they can differ.
            quantity=order.quantity,
            fill_price=order.price,
            mode=self._effective_mode.value,
            client_order_id=client_order_id,
            order_id=order.order_id,
            is_shadow=is_shadow,
            message=message,
            trade_id=trade_id,
            position_id=order.position_id,
            realized_pnl=order.realized_pnl,
            entry_charges=order.entry_charges,
            exit_charges=order.exit_charges,
            is_duplicate=order.status == "DUPLICATE",
        )

    def _record_if_filled(self, idempotency_key: str, result: ExecutionResult) -> None:
        # Record transacted orders so a rejection/block can be retried but a fill can't repeat.
        if result.status in ("FILLED", "PARTIALLY_FILLED"):
            self.idempotency.record(idempotency_key, result.to_dict())

    def _journal_result(
        self,
        *,
        idempotency_key: str,
        requested_price: float,
        order_type: str,
        result: ExecutionResult,
    ) -> None:
        if self.execution_repository is None:
            return
        try:
            self.execution_repository.persist_runtime_result(
                intent_id=idempotency_key,
                requested_price=requested_price,
                order_type=order_type,
                result=result.to_dict(),
            )
        except Exception:
            logger.warning(
                "Execution journal failed; operational paper ledger remains authoritative",
                exc_info=True,
            )

    def submit(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        idempotency_key: str,
        order_type: str = "MARKET",
        trade_id: str = "",
        position_id: str = "",
        stop_loss: float = 0.0,
        target_price: float = 0.0,
        strategy: str = "",
        reason: str = "",
        entry_data_source: str = "real",
    ) -> ExecutionResult:
        """
        Submit synchronously. Handles local_paper and shadow; a live mode returns a reject
        (live submission is async — use submit_async).
        """
        side = side.upper()
        client_order_id = self._client_order_id(idempotency_key)
        guard = self._guard(symbol, side, quantity, idempotency_key, client_order_id)
        if guard is not None:
            self._journal_result(
                idempotency_key=idempotency_key,
                requested_price=price,
                order_type=order_type,
                result=guard,
            )
            return guard

        if self._effective_mode in (ExecutionMode.LOCAL_PAPER, ExecutionMode.SHADOW):
            result = self._fill_paper(
                symbol,
                side,
                quantity,
                price,
                order_type,
                client_order_id,
                idempotency_key,
                trade_id,
                position_id,
                stop_loss,
                target_price,
                strategy,
                reason,
                entry_data_source,
            )
        else:
            result = ExecutionResult(
                status="REJECTED",
                symbol=symbol,
                side=side,
                quantity=quantity,
                fill_price=0.0,
                mode=self._effective_mode.value,
                client_order_id=client_order_id,
                message="live submission requires submit_async (async broker lifecycle)",
            )
        self._record_if_filled(idempotency_key, result)
        self._journal_result(
            idempotency_key=idempotency_key,
            requested_price=price,
            order_type=order_type,
            result=result,
        )
        return result

    async def submit_async(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        idempotency_key: str,
        order_type: str = "MARKET",
        trade_id: str = "",
        position_id: str = "",
        stop_loss: float = 0.0,
        target_price: float = 0.0,
        strategy: str = "",
        reason: str = "",
        entry_data_source: str = "real",
    ) -> ExecutionResult:
        """Submit through the effective mode, awaiting the broker for live modes."""
        side = side.upper()
        client_order_id = self._client_order_id(idempotency_key)
        guard = self._guard(symbol, side, quantity, idempotency_key, client_order_id)
        if guard is not None:
            await asyncio.to_thread(
                self._journal_result,
                idempotency_key=idempotency_key,
                requested_price=price,
                order_type=order_type,
                result=guard,
            )
            return guard

        if self._effective_mode in (ExecutionMode.LOCAL_PAPER, ExecutionMode.SHADOW):
            result = self._fill_paper(
                symbol,
                side,
                quantity,
                price,
                order_type,
                client_order_id,
                idempotency_key,
                trade_id,
                position_id,
                stop_loss,
                target_price,
                strategy,
                reason,
                entry_data_source,
            )
        elif self.broker_executor is None:
            result = ExecutionResult(
                status="REJECTED",
                symbol=symbol,
                side=side,
                quantity=quantity,
                fill_price=0.0,
                mode=self._effective_mode.value,
                client_order_id=client_order_id,
                message="live mode but no broker executor attached",
            )
        else:
            result = await self._submit_live(
                symbol, side, quantity, price, order_type, client_order_id
            )
        self._record_if_filled(idempotency_key, result)
        await asyncio.to_thread(
            self._journal_result,
            idempotency_key=idempotency_key,
            requested_price=price,
            order_type=order_type,
            result=result,
        )
        return result

    async def _submit_live(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        order_type: str,
        client_order_id: str,
    ) -> ExecutionResult:
        """Place a real order via the broker executor and map its confirmed status."""
        assert self.broker_executor is not None
        request = OrderRequest(
            symbol=symbol,
            exchange="NSE",
            side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
            quantity=quantity,
            order_type=OrderType.LIMIT if order_type.upper() == "LIMIT" else OrderType.MARKET,
            price=price,
        )
        confirmed = await self.broker_executor.place_and_confirm(request, client_order_id)
        status_map = {
            OrderStatus.FILLED: "FILLED",
            OrderStatus.PARTIALLY_FILLED: "PARTIALLY_FILLED",
        }
        status = status_map.get(confirmed.status, "REJECTED")
        return ExecutionResult(
            status=status,
            symbol=symbol,
            side=side,
            quantity=confirmed.filled_quantity or quantity,
            fill_price=confirmed.average_price or price,
            mode=self._effective_mode.value,
            client_order_id=client_order_id,
            order_id=confirmed.order_id,
            message=confirmed.message or f"broker status: {confirmed.status.value}",
        )
