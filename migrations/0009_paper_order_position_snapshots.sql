-- Idempotent replay of a paper order must return the position exactly as it
-- was immediately after that order's fill, not the position's current
-- (possibly further-mutated) state. paper.positions is a mutable pointer to
-- the latest state per (market, account_id, symbol); this table is an
-- immutable snapshot per order.
CREATE TABLE IF NOT EXISTS paper.order_positions (
    order_id text PRIMARY KEY REFERENCES paper.orders(order_id),
    position_id text NOT NULL,
    signed_quantity double precision NOT NULL,
    average_entry_price double precision NOT NULL CHECK (average_entry_price >= 0),
    realized_pnl double precision NOT NULL,
    total_fees double precision NOT NULL CHECK (total_fees >= 0),
    opened_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    closed_at timestamptz,
    state text NOT NULL CHECK (state IN ('OPEN', 'CLOSED')),
    decision_ids jsonb NOT NULL,
    strategy_keys jsonb NOT NULL,
    approval_ids jsonb NOT NULL,
    gold_snapshot_ids jsonb NOT NULL,
    agent_evidence_ids jsonb NOT NULL
);
