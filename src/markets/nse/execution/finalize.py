"""Durable-outbox finalize step for a fully-closed canonical trade lifecycle.

Journal close + performance record + lesson crediting for a CLOSED ``trade_id``. Extracted
out of the live-trading loop (rather than left as a closure inline there) for two reasons:

- **Uniform routing.** It is one reason-agnostic code path shared by every close (target,
  stop, trailing stop, time exit, stale exit, regime change, kill-switch flatten). A
  kill-switch flatten calling the same ``TradeFinalizer.finalize`` as a normal close is what
  guarantees it produces the *same complete outcome records* — journal closed, performance
  recorded, lessons credited — instead of a shortcut that skips them (see C-5, Institutional
  Audit).
- **Independent testability.** It depends only on the stores it needs (the lifecycle ledger,
  performance tracker, trade journal, and optionally the learning loop), not on the live
  loop's market-data/dashboard/websocket wiring, so failure-injection tests can exercise
  crash/restart finalize-replay directly.

Idempotent per ``trade_id``: guarded by ``PaperTradeLifecycleStore.mark_finalized`` (and,
belt-and-suspenders, by ``PerformanceTracker.record_trade``'s own trade_id dedup), so it is
always safe to call again — which is exactly what the startup replay loop
(``get_unfinalized_closed``) does for any trade the ledger recorded CLOSED but whose
journal/performance/lesson fan-out never completed because the process crashed in between.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.core.decisions import stable_decision_id
from src.core.execution import stable_execution_id
from src.core.outcomes import OutcomeRecord, SchemaBoundOutcomeRepository
from src.markets.nse.execution.journal import TradeJournal
from src.markets.nse.execution.lifecycle import PaperTradeLifecycleStore
from src.memory import feedback
from src.memory.classifier import ClassifiedMistake, MistakeClassifier
from src.memory.database import AgentMemoryDB
from src.memory.injection import MemoryInjector
from src.memory.performance_tracker import PerformanceTracker

logger = logging.getLogger(__name__)


@dataclass
class TradeFinalizer:
    lifecycle_store: PaperTradeLifecycleStore
    perf_tracker: PerformanceTracker
    journal: TradeJournal
    enable_learning: bool = False
    mistake_classifier: MistakeClassifier | None = None
    memory_injector: MemoryInjector | None = None
    memory_db: AgentMemoryDB | None = None
    # Optional hook for surfacing a learned lesson to a UI/log (kept out of this module so it
    # has no dashboard dependency); called with the ClassifiedMistake when one is produced.
    on_lesson: Callable[[ClassifiedMistake], None] | None = None
    outcome_repository: SchemaBoundOutcomeRepository | None = None

    def finalize(
        self,
        trade_id: str,
        *,
        exit_price: float | None = None,
        exit_reason: str | None = None,
    ) -> bool:
        """Run the journal/performance/learning fan-out for a CLOSED trade.

        Returns True only if this call actually performed the fan-out (False if the
        trade_id is unknown, still open, or was already finalized — including by a
        concurrent/prior call). ``exit_price``/``exit_reason`` default to the ledger's own
        last-known values when omitted, which is what makes a startup replay (no live fill
        result available) work identically to the normal in-cycle call.
        """
        lifecycle = self.lifecycle_store.get(trade_id)
        if lifecycle is None:
            logger.warning("Cannot finalize unknown trade_id=%s", trade_id)
            return False
        if lifecycle.get("status") != "CLOSED":
            logger.warning(
                "Refusing to finalize trade_id=%s: status=%s (must be CLOSED)",
                trade_id,
                lifecycle.get("status"),
            )
            return False
        if lifecycle.get("finalized_at") is not None:
            return False

        cumulative_pnl = float(lifecycle.get("cumulative_pnl", 0.0))
        original_quantity = int(lifecycle.get("original_quantity", 0))
        weighted_entry = float(lifecycle.get("weighted_entry_price", 0.0))
        notional = weighted_entry * original_quantity
        pnl_pct = cumulative_pnl / notional * 100 if notional else 0.0
        symbol = str(lifecycle.get("symbol", ""))
        strategy = str(lifecycle.get("strategy", ""))
        context = lifecycle.get("context") or {}
        regime = str(context.get("regime", ""))
        side = str(lifecycle.get("side", "BUY"))
        stop_loss = float(lifecycle.get("stop_loss", 0.0))
        target_price = float(lifecycle.get("target_price", 0.0))
        mae = float(lifecycle.get("mae", 0.0))
        mfe = float(lifecycle.get("mfe", 0.0))

        if exit_price is None:
            exit_price = float(lifecycle.get("current_price", weighted_entry))
        if exit_reason is None:
            exit_events = [
                e
                for e in self.lifecycle_store.events(trade_id)
                if e["event_type"] in ("EXIT_FILL", "PARTIAL_EXIT_FILL")
            ]
            exit_reason = str(exit_events[-1]["reason"]) if exit_events else "unknown"

        hold_minutes = 0
        created_at = lifecycle.get("created_at")
        closed_at = lifecycle.get("closed_at")
        if created_at is not None and closed_at is not None:
            try:
                hold_minutes = max(0, int((closed_at - created_at).total_seconds() / 60))
            except Exception:
                hold_minutes = 0

        def _aware(value: Any) -> datetime | None:
            if not isinstance(value, datetime):
                return None
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

        if self.outcome_repository is not None:
            signal_id = str(lifecycle.get("signal_id", ""))
            idempotency_key = str(lifecycle.get("idempotency_key", ""))
            feature_snapshot_id = str(context.get("feature_snapshot_id", "")).strip() or None
            outcome = OutcomeRecord.create(
                market="NSE",
                provider="DHAN",
                trade_id=trade_id,
                decision_id=stable_decision_id("NSE", signal_id) if signal_id else None,
                entry_execution_id=(
                    stable_execution_id("NSE", idempotency_key) if idempotency_key else None
                ),
                feature_snapshot_id=feature_snapshot_id,
                symbol=symbol,
                timeframe=str(lifecycle.get("timeframe", "")),
                side=side,
                strategy=strategy,
                opened_at=_aware(created_at),
                closed_at=_aware(closed_at) or datetime.now(UTC),
                entry_price=weighted_entry,
                exit_price=exit_price,
                quantity=original_quantity,
                net_pnl=cumulative_pnl,
                return_pct=pnl_pct,
                mae=mae,
                mfe=mfe,
                hold_minutes=hold_minutes,
                exit_reason=exit_reason,
                attribution={
                    "regime": regime,
                    "rationale": lifecycle.get("rationale", []),
                    "validation": context.get("validation", {}),
                    "risk_result": context.get("risk_result", {}),
                    "active_lessons": lifecycle.get("active_lessons", []),
                },
                payload=lifecycle,
            )
            try:
                self.outcome_repository.persist_many([outcome])
            except Exception:
                # Leave the durable lifecycle unfinalized so startup replay retries this
                # missing Outcome before declaring the fan-out complete.
                logger.warning("Outcome journal failed for %s; finalize will retry", trade_id, exc_info=True)
                return False

        # Aggregate: exactly one performance-tracker outcome per trade_id, covering every
        # partial fill plus the final leg's cumulative net P&L (not one row per leg) — the
        # C-5 "partial exits recorded as if each were a complete trade" fix.
        self.perf_tracker.record_trade(
            strategy=strategy,
            regime=regime,
            pnl=cumulative_pnl,
            pnl_pct=pnl_pct,
            symbol=symbol,
            trade_id=trade_id,
        )
        try:
            self.journal.close_trade(
                trade_id,
                exit_price,
                exit_reason,
                mae=mae,
                mfe=mfe,
                pnl=cumulative_pnl,
                exit_quantity=original_quantity,
            )
        except Exception as exc:
            logger.warning("Journal close_trade failed for %s: %s", trade_id, exc)

        if self.enable_learning and self.mistake_classifier and self.memory_injector:
            outcome = feedback.build_outcome(
                trade_id=trade_id,
                symbol=symbol,
                strategy=strategy,
                regime=regime,
                side=side,
                entry_price=weighted_entry,
                exit_price=exit_price,
                stop_loss=stop_loss,
                target_price=target_price,
                pnl=cumulative_pnl,
                pnl_pct=pnl_pct,
                mae=mae,
                mfe=mfe,
                hold_minutes=hold_minutes,
            )
            mistake = feedback.learn_from_outcome(
                self.memory_injector, self.mistake_classifier, outcome
            )
            if mistake and self.on_lesson is not None:
                try:
                    self.on_lesson(mistake)
                except Exception:  # pragma: no cover - defensive, UI hook must never disrupt
                    logger.debug("on_lesson hook failed (non-fatal)", exc_info=True)
            if self.memory_db is not None:
                feedback.mark_lessons_outcome(
                    self.memory_db,
                    lifecycle.get("active_lessons", []),
                    was_successful=cumulative_pnl > 0,
                )

        self.lifecycle_store.mark_finalized(trade_id)
        return True

    def replay_unfinalized(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """Startup durable-outbox drain: finalize every CLOSED-but-unfinalized trade.

        Returns the lifecycle records that were replayed (before finalization), so the
        caller can log/alert on exactly what a crash left pending.
        """
        pending = self.lifecycle_store.get_unfinalized_closed(namespace)
        for record in pending:
            self.finalize(str(record["trade_id"]))
        return pending
