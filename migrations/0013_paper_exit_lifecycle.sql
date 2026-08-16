CREATE TABLE IF NOT EXISTS paper.exit_plans (
    position_id text PRIMARY KEY REFERENCES paper.positions(position_id),
    market text NOT NULL CHECK (market IN ('nse', 'forex', 'crypto')),
    account_id text NOT NULL,
    symbol text NOT NULL,
    entry_action text NOT NULL CHECK (entry_action IN ('BUY', 'SELL')),
    quantity double precision NOT NULL CHECK (quantity > 0),
    stop_price double precision NOT NULL CHECK (stop_price > 0),
    target_price double precision NOT NULL CHECK (target_price > 0),
    allocated_capital double precision NOT NULL CHECK (allocated_capital > 0),
    candidate_id text NOT NULL,
    approval_id text NOT NULL REFERENCES research.strategy_approvals(approval_id),
    strategy_key text NOT NULL REFERENCES research.strategy_definitions(strategy_key),
    gold_snapshot_ids jsonb NOT NULL,
    state text NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE', 'CLOSED')),
    exit_reason text CHECK (exit_reason IN ('STOP', 'TARGET')),
    created_at timestamptz NOT NULL,
    closed_at timestamptz
);

CREATE INDEX IF NOT EXISTS paper_active_exit_plans_idx
    ON paper.exit_plans (market, account_id) WHERE state = 'ACTIVE';
