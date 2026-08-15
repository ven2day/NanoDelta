"""
Tests for the unified ExecutionService: mode resolution (incl. shadow + no silent
downgrade), order idempotency, the kill-switch gate, and the IdempotencyStore.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.markets.nse.execution.paper_engine import LocalPaperEngine
from src.markets.nse.execution.service import (
    ExecutionMode,
    ExecutionService,
    IdempotencyStore,
)
from src.markets.nse.risk.costs import CostModel


def _unique_sqlite_url(tmp_path) -> str:
    """A per-call sqlite file — NOT a bare ':memory:' URL, since the shared engine
    cache in src/db/base.py keys by URL string: every test using the literal
    ':memory:' would otherwise share one cached (and thus one leaking) database."""
    return f"sqlite:///{tmp_path}/idem-{uuid.uuid4().hex}.db"


@pytest.fixture
def engine(tmp_path):
    return LocalPaperEngine(
        initial_balance=100_000.0,
        database_url=f"sqlite:///{tmp_path}/wallet.db",
        cost_model=CostModel.zero(),
    )


def _service(engine, tmp_path, **kwargs):
    kwargs.setdefault("idempotency", IdempotencyStore(database_url=_unique_sqlite_url(tmp_path)))
    return ExecutionService(engine=engine, **kwargs)


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------


def test_local_paper_fills(engine, tmp_path):
    svc = _service(engine, tmp_path, mode=ExecutionMode.LOCAL_PAPER)
    result = svc.submit(symbol="AAPL", side="BUY", quantity=10, price=100.0, idempotency_key="k1")
    assert result.filled
    assert result.is_shadow is False
    assert len(engine.get_positions()) == 1


def test_local_paper_fill_publishes_execution_journal_result(engine, tmp_path):
    class _ExecutionSink:
        def __init__(self):
            self.records = []

        def persist_runtime_result(self, **kwargs):
            self.records.append(kwargs)
            return 1

    sink = _ExecutionSink()
    svc = _service(
        engine,
        tmp_path,
        mode=ExecutionMode.LOCAL_PAPER,
        execution_repository=sink,
    )

    result = svc.submit(
        symbol="AAPL",
        side="BUY",
        quantity=10,
        price=100.0,
        idempotency_key="journal-k1",
    )

    assert result.filled
    assert len(sink.records) == 1
    assert sink.records[0]["intent_id"] == "journal-k1"
    assert sink.records[0]["result"]["order_id"] == result.order_id


def test_live_without_opt_in_runs_shadow(engine, tmp_path):
    svc = _service(engine, tmp_path, mode=ExecutionMode.LIVE, allow_live_orders=False)
    assert svc.effective_mode == ExecutionMode.SHADOW
    result = svc.submit(symbol="AAPL", side="BUY", quantity=10, price=100.0, idempotency_key="k1")
    assert result.filled  # simulated
    assert result.is_shadow is True
    assert "SHADOW" in result.message


def test_dhan_paper_without_opt_in_runs_shadow(engine, tmp_path):
    svc = _service(engine, tmp_path, mode=ExecutionMode.DHAN_PAPER, allow_live_orders=False)
    assert svc.effective_mode == ExecutionMode.SHADOW


def test_c2_hazardous_combination_cannot_reach_real_broker_submission(engine, tmp_path):
    """Regression test for C-2 (docs/audits/DeltaQuant-Quant-Risk-Review.md).

    The audited hazard: TRADING_MODE=paper, EXECUTION_MODE=dhan_paper,
    ALLOW_LIVE_ORDERS=true, with valid Dhan credentials, used to resolve to a
    broker-capable mode (ExecutionService._resolve_mode only checked execution_mode +
    allow_live_orders + credentials, never trading_mode). dhan_paper is now redefined
    to simulate Dhan-shaped data only and can NEVER reach a live route, so this exact
    combination must resolve to SHADOW even with a real (attached-but-unused) broker
    executor -- and submitting through it must never touch that broker executor.
    """
    fake_settings = MagicMock(
        trading_mode="paper",  # every label says paper...
        dhan_client_id="real-client-id",
        dhan_access_token="real-access-token",  # ...and credentials are genuinely valid
    )
    broker_executor = MagicMock()
    broker_executor.place_and_confirm = AsyncMock()

    with patch("src.markets.nse.execution.service.get_settings", return_value=fake_settings):
        svc = ExecutionService(
            engine=engine,
            mode=ExecutionMode.DHAN_PAPER,
            allow_live_orders=True,  # ...and the master live-order gate is armed
            broker_executor=broker_executor,
            idempotency=IdempotencyStore(database_url=_unique_sqlite_url(tmp_path)),
        )
        assert svc.effective_mode == ExecutionMode.SHADOW
        assert svc.real_orders_active is False

        result = asyncio.run(
            svc.submit_async(
                symbol="RELIANCE",
                side="BUY",
                quantity=10,
                price=2500.0,
                idempotency_key="c2-hazard",
            )
        )

    assert result.is_shadow is True
    assert "SHADOW" in result.message
    broker_executor.place_and_confirm.assert_not_called()


def test_live_mode_requires_trading_mode_live_independent_of_allow_live_orders(engine, tmp_path):
    """_resolve_mode must independently require trading_mode=='live', not just
    execution_mode + allow_live_orders + credentials -- the missing check C-2 identified."""
    fake_settings = MagicMock(
        trading_mode="paper",
        dhan_client_id="real-client-id",
        dhan_access_token="real-access-token",
    )
    with patch("src.markets.nse.execution.service.get_settings", return_value=fake_settings):
        svc = ExecutionService(
            engine=engine,
            mode=ExecutionMode.LIVE,
            allow_live_orders=True,
            idempotency=IdempotencyStore(database_url=_unique_sqlite_url(tmp_path)),
        )
        assert svc.effective_mode == ExecutionMode.SHADOW
        assert svc.real_orders_active is False


def test_real_orders_active_true_only_for_full_conjunction(engine, tmp_path):
    fake_settings = MagicMock(
        trading_mode="live",
        dhan_client_id="real-client-id",
        dhan_access_token="real-access-token",
    )
    broker_executor = MagicMock()
    with patch("src.markets.nse.execution.service.get_settings", return_value=fake_settings):
        svc = ExecutionService(
            engine=engine,
            mode=ExecutionMode.LIVE,
            allow_live_orders=True,
            broker_executor=broker_executor,
            idempotency=IdempotencyStore(database_url=_unique_sqlite_url(tmp_path)),
        )
        assert svc.effective_mode == ExecutionMode.LIVE
        assert svc.real_orders_active is True


def test_live_with_opt_in_but_no_creds_runs_shadow_not_local(engine, tmp_path):
    # allow_live_orders=True but no Dhan creds -> SHADOW (NOT a silent local-paper downgrade).
    svc = _service(engine, tmp_path, mode=ExecutionMode.LIVE, allow_live_orders=True)
    assert svc.effective_mode == ExecutionMode.SHADOW


def test_live_via_sync_submit_rejects_directs_to_async(engine, tmp_path):
    # Live orders are async (broker lifecycle); the sync submit() must refuse, never silently
    # fill the paper wallet. Real live submission goes through submit_async (see live tests).
    fake_settings = MagicMock(dhan_client_id="id", dhan_access_token="tok", trading_mode="live")
    with patch("src.markets.nse.execution.service.get_settings", return_value=fake_settings):
        svc = ExecutionService(
            engine=engine,
            mode=ExecutionMode.LIVE,
            allow_live_orders=True,
            idempotency=IdempotencyStore(database_url=_unique_sqlite_url(tmp_path)),
        )
        assert svc.effective_mode == ExecutionMode.LIVE
        result = svc.submit(
            symbol="AAPL", side="BUY", quantity=10, price=100.0, idempotency_key="k1"
        )
    assert result.status == "REJECTED"
    assert "submit_async" in result.message
    assert len(engine.get_positions()) == 0


# ---------------------------------------------------------------------------
# Idempotency + kill switch
# ---------------------------------------------------------------------------


def test_duplicate_submission_is_suppressed(engine, tmp_path):
    svc = _service(engine, tmp_path, mode=ExecutionMode.LOCAL_PAPER)
    first = svc.submit(symbol="AAPL", side="BUY", quantity=10, price=100.0, idempotency_key="dup")
    second = svc.submit(symbol="AAPL", side="BUY", quantity=10, price=100.0, idempotency_key="dup")

    assert first.filled
    assert second.is_duplicate
    assert second.status == "DUPLICATE"
    # Engine only saw the first order — no double position / double spend.
    assert len(engine.get_positions()) == 1
    assert engine.get_balance() == 100_000.0 - 10 * 100.0


def test_kill_switch_blocks_submission(engine, tmp_path):
    svc = _service(engine, tmp_path, mode=ExecutionMode.LOCAL_PAPER, kill_switch=lambda: True)
    result = svc.submit(symbol="AAPL", side="BUY", quantity=10, price=100.0, idempotency_key="k")
    assert result.status == "BLOCKED"
    assert len(engine.get_positions()) == 0  # nothing placed


def test_rejected_order_is_not_recorded_as_duplicate(engine, tmp_path):
    # First order rejected (insufficient balance) -> the key can be retried later.
    svc = _service(engine, tmp_path, mode=ExecutionMode.LOCAL_PAPER)
    rejected = svc.submit(
        symbol="AAPL", side="BUY", quantity=100_000, price=100.0, idempotency_key="retry"
    )
    assert rejected.status == "REJECTED"
    retry = svc.submit(symbol="AAPL", side="BUY", quantity=1, price=100.0, idempotency_key="retry")
    assert retry.filled
    assert retry.is_duplicate is False


# ---------------------------------------------------------------------------
# IdempotencyStore persistence
# ---------------------------------------------------------------------------


def test_idempotency_store_persists(tmp_path):
    database_url = f"sqlite:///{tmp_path}/idem.db"
    store = IdempotencyStore(database_url=database_url)
    store.record("k1", {"fill_price": 100.0, "order_id": "O1"})

    reloaded = IdempotencyStore(database_url=database_url)
    assert reloaded.seen("k1") is not None
    assert reloaded.seen("k1")["order_id"] == "O1"
    assert reloaded.seen("missing") is None
