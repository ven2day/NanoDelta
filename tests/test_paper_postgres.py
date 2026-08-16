"""Behavioral tests for PostgresPaperExecutionEngine, including cross-restart
durability and the three correctness properties a synchronous in-memory-only
engine cannot provide: rollback must restore in-memory state, concurrent fills
on the same symbol must be serialized, and idempotent replay must return the
position as of that specific fill rather than whatever it has since become.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from nanodelta.agents import AdvisoryAction
from nanodelta.contracts import Market
from nanodelta.paper import ExecutionPolicy, PositionState, PostgresPaperExecutionEngine
from nanodelta.risk import PortfolioSnapshot, RiskDecisionState, RiskEngine, RiskLimits, TradeIntent
from nanodelta.strategies import StrategyApproval, StrategyIdentity

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
IDENTITY = StrategyIdentity(Market.NSE, "vwap_pullback", "1.0.0", "5m", "30m", 1)


def approval() -> StrategyApproval:
    return StrategyApproval.create(
        identity=IDENTITY,
        validation_run_id="validation-1",
        approved_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=30),
        approved_by="committee",
        reason="passed",
    )


def intent(
    action: AdvisoryAction = AdvisoryAction.BUY,
    *,
    quantity: float = 10,
    reference_price: float = 100,
    suffix: str = "1",
) -> TradeIntent:
    artifact = approval()
    return TradeIntent(
        f"intent-{suffix}",
        Market.NSE,
        "paper-1",
        "RELIANCE",
        action,
        quantity,
        reference_price,
        NOW,
        f"candidate-{suffix}",
        artifact.approval_id,
        IDENTITY,
        (f"gold-{suffix}",),
        None,
    )


def portfolio(*positions: object, pnl: float = 0) -> PortfolioSnapshot:
    return PortfolioSnapshot("snapshot-1", "paper-1", 100_000, pnl, positions, NOW)  # type: ignore[arg-type]


def limits() -> RiskLimits:
    return RiskLimits(10_000, 20_000, 50_000, 80_000, 2_000, 5)


def approved_decision(
    action: AdvisoryAction = AdvisoryAction.BUY,
    *,
    quantity: float = 10,
    reference_price: float = 100,
    suffix: str = "1",
    snapshot: PortfolioSnapshot | None = None,
):
    decision = RiskEngine(limits()).evaluate(
        intent(action, quantity=quantity, reference_price=reference_price, suffix=suffix),
        approval(),
        snapshot or portfolio(),
        evaluated_at=NOW,
    )
    assert decision.state is RiskDecisionState.APPROVED
    return decision


class FakePaperDatabase:
    def __init__(self) -> None:
        self.decisions: dict[str, dict[str, object]] = {}
        self.orders: dict[str, dict[str, object]] = {}
        self.fills: dict[str, dict[str, object]] = {}
        self.positions: dict[str, dict[str, object]] = {}
        self.order_positions: dict[str, dict[str, object]] = {}
        self.realizations: dict[str, dict[str, object]] = {}
        # lock_key -> id() of the FakeConnection currently holding it (only
        # while that connection hasn't committed/rolled back yet).
        self.locks: dict[str, int] = {}

    def connect(self) -> FakeConnection:
        return FakeConnection(self)


class FakeConnection:
    """Stages writes and only merges them into the shared database on commit(),
    discarding them on rollback() -- real transactional semantics matter here
    because _execute_one performs several sequential INSERTs, and a naive fake
    that wrote straight into the shared dicts would make a rollback-on-failure
    test pass even if the real adapter left partial state behind. Also tracks
    advisory-lock ownership: a second connection trying to lock a key already
    held by another open connection raises, standing in for the real blocking
    behavior `pg_advisory_xact_lock` would have -- enough to prove the code
    acquires the lock before reading state, without needing real threads."""

    def __init__(self, db: FakePaperDatabase) -> None:
        self.db = db
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.staged_decisions: dict[str, dict[str, object]] = {}
        self.staged_orders: dict[str, dict[str, object]] = {}
        self.staged_fills: dict[str, dict[str, object]] = {}
        self.staged_positions: dict[str, dict[str, object]] = {}
        self.staged_order_positions: dict[str, dict[str, object]] = {}
        self.staged_realizations: dict[str, dict[str, object]] = {}
        self.held_locks: set[str] = set()

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1
        self.db.decisions.update(self.staged_decisions)
        self.db.orders.update(self.staged_orders)
        self.db.fills.update(self.staged_fills)
        self.db.positions.update(self.staged_positions)
        self.db.order_positions.update(self.staged_order_positions)
        self.db.realizations.update(self.staged_realizations)
        self._release_locks()

    def rollback(self) -> None:
        self.rollbacks += 1
        self.staged_decisions.clear()
        self.staged_orders.clear()
        self.staged_fills.clear()
        self.staged_positions.clear()
        self.staged_order_positions.clear()
        self.staged_realizations.clear()
        self._release_locks()

    def _release_locks(self) -> None:
        for key in self.held_locks:
            if self.db.locks.get(key) == id(self):
                del self.db.locks[key]
        self.held_locks.clear()

    def close(self) -> None:
        self.closed = True


def _position_row(record: dict[str, object]) -> tuple[object, ...]:
    return (
        record["position_id"],
        record["market"],
        record["account_id"],
        record["symbol"],
        record["signed_quantity"],
        record["average_entry_price"],
        record["realized_pnl"],
        record["total_fees"],
        record["opened_at"],
        record["updated_at"],
        record["closed_at"],
        record["state"],
        record["decision_ids"],
        record["strategy_keys"],
        record["approval_ids"],
        record["gold_snapshot_ids"],
        record["agent_evidence_ids"],
    )


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self._one: tuple[object, ...] | None = None
        self._all: list[tuple[object, ...]] = []

    def _view(self, name: str) -> dict[str, dict[str, object]]:
        """Read-your-own-writes: committed rows plus this connection's staged ones."""
        merged = dict(getattr(self.connection.db, name))
        merged.update(getattr(self.connection, f"staged_{name}"))
        return merged

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self._one = None
        self._all = []
        if query.startswith("SELECT pg_advisory_xact_lock"):
            (lock_key,) = params
            holder = self.connection.db.locks.get(lock_key)
            if holder is not None and holder != id(self.connection):
                raise RuntimeError(f"would block: {lock_key} is held by another transaction")
            self.connection.db.locks[lock_key] = id(self.connection)
            self.connection.held_locks.add(lock_key)
        elif query.startswith("INSERT INTO paper.decisions"):
            key = str(params[0])
            if key not in self._view("decisions"):
                self.connection.staged_decisions[key] = {"decision_id": params[0]}
        elif query.startswith("INSERT INTO paper.orders"):
            key = str(params[1])  # idempotency_key
            if key not in self._view("orders"):
                self.connection.staged_orders[key] = {
                    "order_id": params[0],
                    "idempotency_key": params[1],
                    "decision_id": params[2],
                    "market": params[3],
                    "account_id": params[4],
                    "symbol": params[5],
                    "action": params[6],
                    "quantity": params[7],
                    "state": params[8],
                    "submitted_at": params[9],
                }
        elif query.startswith("INSERT INTO paper.fills"):
            order_id = str(params[1])
            if order_id not in self._view("fills"):
                self.connection.staged_fills[order_id] = {
                    "fill_id": params[0],
                    "order_id": params[1],
                    "quantity": params[2],
                    "price": params[3],
                    "fee": params[4],
                    "filled_at": params[5],
                }
        elif query.startswith("INSERT INTO paper.order_positions"):
            order_id = str(params[0])
            if order_id not in self._view("order_positions"):
                self.connection.staged_order_positions[order_id] = {
                    "order_id": params[0],
                    "position_id": params[1],
                    "signed_quantity": params[2],
                    "average_entry_price": params[3],
                    "realized_pnl": params[4],
                    "total_fees": params[5],
                    "opened_at": params[6],
                    "updated_at": params[7],
                    "closed_at": params[8],
                    "state": params[9],
                    "decision_ids": json.loads(str(params[10])),
                    "strategy_keys": json.loads(str(params[11])),
                    "approval_ids": json.loads(str(params[12])),
                    "gold_snapshot_ids": json.loads(str(params[13])),
                    "agent_evidence_ids": json.loads(str(params[14])),
                }
        elif query.startswith("INSERT INTO paper.positions"):
            self.connection.staged_positions[str(params[0])] = {
                "position_id": params[0],
                "market": params[1],
                "account_id": params[2],
                "symbol": params[3],
                "signed_quantity": params[4],
                "average_entry_price": params[5],
                "realized_pnl": params[6],
                "total_fees": params[7],
                "opened_at": params[8],
                "updated_at": params[9],
                "closed_at": params[10],
                "state": params[11],
                "decision_ids": json.loads(str(params[12])),
                "strategy_keys": json.loads(str(params[13])),
                "approval_ids": json.loads(str(params[14])),
                "gold_snapshot_ids": json.loads(str(params[15])),
                "agent_evidence_ids": json.loads(str(params[16])),
            }
        elif query.startswith("INSERT INTO paper.realization_events"):
            fill_id = str(params[1])
            if fill_id not in self._view("realizations"):
                self.connection.staged_realizations[fill_id] = {
                    "event_id": params[0],
                    "fill_id": params[1],
                    "position_id": params[2],
                    "market": params[3],
                    "account_id": params[4],
                    "symbol": params[5],
                    "gross_pnl_delta": params[6],
                    "fee": params[7],
                    "net_pnl": params[8],
                    "realized_at": params[9],
                }
        elif query.startswith("SELECT position_id"):
            market, account_id, symbol = params
            match = next(
                (
                    record
                    for record in self._view("positions").values()
                    if record["market"] == market
                    and record["account_id"] == account_id
                    and record["symbol"] == symbol
                    and record["state"] == "OPEN"
                ),
                None,
            )
            self._one = _position_row(match) if match is not None else None
        elif query.startswith("SELECT o.order_id"):
            (idempotency_key,) = params
            order = self._view("orders").get(str(idempotency_key))
            if order is None:
                return
            fill = self._view("fills").get(str(order["order_id"]))
            if fill is None:
                return
            snapshot = self._view("order_positions").get(str(order["order_id"]))
            if snapshot is None:
                return
            self._one = (
                order["order_id"],
                order["idempotency_key"],
                order["decision_id"],
                order["market"],
                order["account_id"],
                order["symbol"],
                order["action"],
                order["quantity"],
                order["state"],
                order["submitted_at"],
                fill["fill_id"],
                fill["quantity"],
                fill["price"],
                fill["fee"],
                fill["filled_at"],
                snapshot["position_id"],
                order["market"],
                order["account_id"],
                order["symbol"],
                snapshot["signed_quantity"],
                snapshot["average_entry_price"],
                snapshot["realized_pnl"],
                snapshot["total_fees"],
                snapshot["opened_at"],
                snapshot["updated_at"],
                snapshot["closed_at"],
                snapshot["state"],
                snapshot["decision_ids"],
                snapshot["strategy_keys"],
                snapshot["approval_ids"],
                snapshot["gold_snapshot_ids"],
                snapshot["agent_evidence_ids"],
            )
        else:
            raise AssertionError(f"unexpected query: {query}")

    def fetchone(self) -> tuple[object, ...] | None:
        return self._one

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._all


def test_second_replica_sees_the_receipt_and_position_written_by_the_first() -> None:
    db = FakePaperDatabase()
    engine = PostgresPaperExecutionEngine(ExecutionPolicy(slippage_bps=10, fee_bps=5), db.connect)
    decision = approved_decision()

    first = engine.execute(decision, idempotency_key="order-key-1", executed_at=NOW)

    second_replica = PostgresPaperExecutionEngine(ExecutionPolicy(10, 5), db.connect)
    replayed = second_replica.execute(decision, idempotency_key="order-key-1", executed_at=NOW)

    assert replayed == first
    assert replayed.position.signed_quantity == 10
    assert replayed.position.state is PositionState.OPEN


def test_position_nets_correctly_across_a_restart() -> None:
    """The property PaperExecutionEngine cannot provide at all: a second process,
    with an empty in-memory cache, must still see the real open position to net
    against -- otherwise a restart mid-position would silently open a second,
    phantom position instead of adding to or closing the real one."""
    db = FakePaperDatabase()
    opener = PostgresPaperExecutionEngine(ExecutionPolicy(0, 0), db.connect)
    opened = opener.execute(approved_decision(), idempotency_key="open", executed_at=NOW).position

    snapshot = portfolio(
        _fake_position(opened.market, opened.account_id, opened.symbol, opened.signed_quantity, 110)
    )
    close_decision = approved_decision(
        AdvisoryAction.SELL, quantity=10, reference_price=110, suffix="2", snapshot=snapshot
    )

    restarted_process = PostgresPaperExecutionEngine(ExecutionPolicy(0, 0), db.connect)
    closed = restarted_process.execute(
        close_decision, idempotency_key="close", executed_at=NOW + timedelta(minutes=5)
    ).position

    assert closed.position_id == opened.position_id
    assert closed.state is PositionState.CLOSED
    assert closed.signed_quantity == 0
    assert closed.realized_pnl == pytest.approx(100)
    close_fill = next(row for row in db.realizations.values() if row["gross_pnl_delta"] == 100)
    assert close_fill["net_pnl"] == pytest.approx(100)


def _fake_position(
    market: Market,
    account_id: str,
    symbol: str,
    signed_quantity: float,
    mark_price: float,
):
    from nanodelta.risk import PortfolioPosition

    return PortfolioPosition(market, account_id, symbol, signed_quantity, mark_price)


def test_rejected_risk_decision_still_cannot_enter_paper_execution() -> None:
    db = FakePaperDatabase()
    rejected = RiskEngine(RiskLimits(1, 1, 1, 1, 1, 1)).evaluate(
        intent(), approval(), portfolio(), evaluated_at=NOW
    )
    engine = PostgresPaperExecutionEngine(ExecutionPolicy(0, 0), db.connect)
    with pytest.raises(PermissionError, match="approved risk decision"):
        engine.execute(rejected, idempotency_key="blocked", executed_at=NOW)
    assert db.decisions == {}
    assert db.orders == {}


def test_execute_rolls_back_every_table_when_a_later_write_fails() -> None:
    db = FakePaperDatabase()

    class FailingConnection(FakeConnection):
        def cursor(self) -> FakeCursor:
            cursor = super().cursor()
            original = cursor.execute

            def execute(query: str, params: tuple[object, ...] = ()) -> None:
                if query.startswith("INSERT INTO paper.positions"):
                    raise RuntimeError("boom")
                original(query, params)

            cursor.execute = execute  # type: ignore[method-assign]
            return cursor

    engine = PostgresPaperExecutionEngine(ExecutionPolicy(0, 0), lambda: FailingConnection(db))
    with pytest.raises(RuntimeError, match="boom"):
        engine.execute(approved_decision(), idempotency_key="doomed", executed_at=NOW)

    assert db.decisions == {}
    assert db.orders == {}
    assert db.fills == {}
    assert db.positions == {}
    assert db.order_positions == {}
    assert db.realizations == {}


def test_rollback_restores_in_memory_state_not_just_the_database() -> None:
    """Bug fixed after automated review on #34: super().execute() mutates the
    base class's in-memory _receipts/_positions before this class persists
    anything. A failed write must undo those mutations too, or a later call in
    the same process builds on phantom state the database never actually has."""
    db = FakePaperDatabase()

    class FailingConnection(FakeConnection):
        def cursor(self) -> FakeCursor:
            cursor = super().cursor()
            original = cursor.execute

            def execute(query: str, params: tuple[object, ...] = ()) -> None:
                if query.startswith("INSERT INTO paper.order_positions"):
                    raise RuntimeError("boom")
                original(query, params)

            cursor.execute = execute  # type: ignore[method-assign]
            return cursor

    engine = PostgresPaperExecutionEngine(ExecutionPolicy(0, 0), lambda: FailingConnection(db))
    with pytest.raises(RuntimeError, match="boom"):
        engine.execute(approved_decision(), idempotency_key="doomed", executed_at=NOW)

    assert engine._receipts == {}
    assert engine._positions == {}


def test_concurrent_fills_on_the_same_symbol_are_serialized_by_an_advisory_lock() -> None:
    db = FakePaperDatabase()
    engine = PostgresPaperExecutionEngine(ExecutionPolicy(0, 0), db.connect)

    # Simulate another, still-open transaction already holding the lock for
    # this exact (market, account_id, symbol) -- e.g. a concurrent worker
    # mid-fill on the same position.
    holder = db.connect()
    holder.cursor().execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("nse:paper-1:RELIANCE",)
    )

    with pytest.raises(RuntimeError, match="would block"):
        engine.execute(approved_decision(), idempotency_key="contended", executed_at=NOW)
    # The failed attempt must not have taken the lock for itself.
    assert db.locks["nse:paper-1:RELIANCE"] == id(holder)

    holder.commit()  # releases the lock
    holder.close()

    receipt = engine.execute(approved_decision(), idempotency_key="contended", executed_at=NOW)
    assert receipt.position.signed_quantity == 10


def test_idempotent_replay_returns_the_position_as_of_that_fill_not_a_later_one() -> None:
    """Bug fixed after automated review on #34: replaying an idempotency key
    used to join to paper.positions' current (mutable) row, so a key retried
    after further fills on the same symbol returned a different position each
    time. It must always return the position exactly as it was right after
    its own fill."""
    db = FakePaperDatabase()
    engine = PostgresPaperExecutionEngine(ExecutionPolicy(0, 0), db.connect)

    first = engine.execute(approved_decision(), idempotency_key="k1", executed_at=NOW)
    assert first.position.signed_quantity == 10

    engine.execute(
        approved_decision(quantity=5, suffix="2"),
        idempotency_key="k2",
        executed_at=NOW + timedelta(minutes=1),
    )
    assert db.positions[first.position.position_id]["signed_quantity"] == 15

    replayed = engine.execute(approved_decision(), idempotency_key="k1", executed_at=NOW)
    assert replayed == first
    assert replayed.position.signed_quantity == 10


def test_partial_close_persists_daily_realization_event() -> None:
    db = FakePaperDatabase()
    engine = PostgresPaperExecutionEngine(ExecutionPolicy(0, 10), db.connect)
    opened = engine.execute(
        approved_decision(quantity=10), idempotency_key="open", executed_at=NOW
    ).position
    snapshot = portfolio(
        _fake_position(opened.market, opened.account_id, opened.symbol, opened.signed_quantity, 110)
    )
    engine.execute(
        approved_decision(
            AdvisoryAction.SELL,
            quantity=4,
            reference_price=110,
            suffix="partial-close",
            snapshot=snapshot,
        ),
        idempotency_key="partial-close",
        executed_at=NOW + timedelta(minutes=5),
    )

    event = next(row for row in db.realizations.values() if row["gross_pnl_delta"] == 40)
    assert event["fee"] == pytest.approx(0.44)
    assert event["net_pnl"] == pytest.approx(39.56)
