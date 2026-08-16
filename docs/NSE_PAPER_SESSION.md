# Continuous NSE paper session

The NSE realtime worker now invokes the governed paper lifecycle whenever a configured,
settled candle materializes a Gold feature record. This path remains paper-only:
no component in it owns or calls a broker order API.

## Runtime path

```text
TrueData (Dhan fallback) quote
  -> settled configured candle
  -> Bronze / Silver / Gold
  -> current exact-identity strategy approval
  -> immutable BUY or SELL candidate
  -> scoring and portfolio allocation
  -> deterministic risk
  -> immediate paper order and fill
  -> durable paper position and protective exit plan
  -> later settled mark triggers stop or target
  -> opposite-side paper fill, closed position and immutable outcome
```

Only a strategy already admitted by `StrategyRegistry.eligible()` can create a candidate.
The session does not create, extend or bypass validation and approval artifacts. When no
compatible current approval exists, the decision ledger records `NO_APPROVED_STRATEGY`
and no paper order is created.

## Session and exit behavior

NSE entry admission uses the existing NSE equity-session policy. Position management runs
before entry preconditions, so stop and target exits continue when the entry session is
closed, the entry kill switch is active, or new capital is unavailable. A symbol closed by
position management is excluded from entry generation in the same cycle.

## Replay and restart safety

`control.paper_session_cycles` is the durable settled-Gold processing ledger. Its cycle key
is derived from the paper account and immutable Gold record IDs. Each cycle has a bounded
lease, attempt count, state, failure type and aggregate decision/order/exit counts.

Decision-cycle IDs no longer depend on process wall-clock time. Candidate, risk-intent and
paper-order keys are consequently stable when the same settled input is retried. Completed
cycles are returned as replays without invoking strategies or execution again.

Failed cycles and expired `RUNNING` leases are reloaded from durable NSE Gold snapshots after
the next successfully processed settled cycle. A decision/database failure is recorded by the
paper session and decision metric; it is not misclassified as a TrueData/Dhan stream failure,
so it cannot trigger an unnecessary market-data provider failover.

The NSE cycle also reloads the immediately preceding settled Silver candle when its in-memory
bar history is empty after process startup. The first new settled candle can therefore produce
Gold and enter the governed lifecycle; restart no longer creates an avoidable one-bar blind spot.

There is one unavoidable cross-transaction boundary: the entry fill commits before its
protective exit plan is registered. Before every claimed cycle, the PostgreSQL session store
reconciles any open NSE paper position missing a plan from its immutable position, risk and
candidate lineage. This makes a crash in that window recoverable on the next settled mark.

## Operational evidence

In-process `PaperSessionHealth` exposes completed, replayed, busy, failed and recovered-plan
counts plus the last cycle, completion time and error class. The durable source remains
`control.paper_session_cycles`; operators should alert on:

- `FAILED` rows or an increasing `attempt_count`;
- `RUNNING` rows whose `locked_until` is in the past;
- an NSE session with new settled Gold rows but no recent completed cycle;
- open `paper.positions` without a matching active `paper.exit_plans` row.

The deterministic integration test covers BUY candidate -> risk -> BUY paper fill -> open
position -> closed-session target -> SELL paper fill -> closed position -> outcome, including
a process restart and replay. A credentialed TrueData/Dhan and TimescaleDB soak remains an
acceptance activity; this code change does not claim that evidence.

## Rollback

Stop the market runtime before rolling back application images. Migration `0017` is additive
and may remain installed. Older images ignore the session ledger but also lose its replay and
recovery guarantees, so do not restart NSE paper processing on an older image while retaining
open paper positions unless every position has an active exit plan.
