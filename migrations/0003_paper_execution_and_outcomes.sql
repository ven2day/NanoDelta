CREATE TABLE IF NOT EXISTS paper.decisions (
    decision_id text PRIMARY KEY,
    intent_id text NOT NULL,
    market text NOT NULL CHECK (market IN ('nse', 'forex', 'crypto')),
    account_id text NOT NULL,
    symbol text NOT NULL,
    action text NOT NULL CHECK (action IN ('BUY', 'SELL')),
    quantity double precision NOT NULL CHECK (quantity > 0),
    reference_price double precision NOT NULL CHECK (reference_price > 0),
    candidate_id text NOT NULL,
    approval_id text NOT NULL REFERENCES research.strategy_approvals(approval_id),
    portfolio_snapshot_id text NOT NULL,
    state text NOT NULL CHECK (state IN ('APPROVED', 'REJECTED')),
    rejection_reasons jsonb NOT NULL,
    limits jsonb NOT NULL,
    gold_snapshot_ids jsonb NOT NULL,
    agent_evidence_id text REFERENCES research.agent_evidence(evidence_id),
    evaluated_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS paper.orders (
    order_id text PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    decision_id text NOT NULL REFERENCES paper.decisions(decision_id),
    market text NOT NULL CHECK (market IN ('nse', 'forex', 'crypto')),
    account_id text NOT NULL,
    symbol text NOT NULL,
    action text NOT NULL CHECK (action IN ('BUY', 'SELL')),
    quantity double precision NOT NULL CHECK (quantity > 0),
    state text NOT NULL CHECK (state = 'FILLED'),
    execution_mode text NOT NULL DEFAULT 'PAPER' CHECK (execution_mode = 'PAPER'),
    submitted_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS paper.fills (
    fill_id text PRIMARY KEY,
    order_id text NOT NULL UNIQUE REFERENCES paper.orders(order_id),
    quantity double precision NOT NULL CHECK (quantity > 0),
    price double precision NOT NULL CHECK (price > 0),
    fee double precision NOT NULL CHECK (fee >= 0),
    filled_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS paper.positions (
    position_id text PRIMARY KEY,
    market text NOT NULL CHECK (market IN ('nse', 'forex', 'crypto')),
    account_id text NOT NULL,
    symbol text NOT NULL,
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

CREATE UNIQUE INDEX IF NOT EXISTS paper_open_position_idx
    ON paper.positions (market, account_id, symbol) WHERE state = 'OPEN';

CREATE TABLE IF NOT EXISTS paper.outcomes (
    outcome_id text PRIMARY KEY,
    position_id text NOT NULL UNIQUE REFERENCES paper.positions(position_id),
    market text NOT NULL CHECK (market IN ('nse', 'forex', 'crypto')),
    account_id text NOT NULL,
    symbol text NOT NULL,
    strategy_key text NOT NULL REFERENCES research.strategy_definitions(strategy_key),
    opened_at timestamptz NOT NULL,
    closed_at timestamptz NOT NULL,
    gross_pnl double precision NOT NULL,
    total_fees double precision NOT NULL CHECK (total_fees >= 0),
    net_pnl double precision NOT NULL,
    return_on_allocated_capital double precision NOT NULL,
    decision_ids jsonb NOT NULL,
    approval_ids jsonb NOT NULL,
    gold_snapshot_ids jsonb NOT NULL,
    agent_evidence_ids jsonb NOT NULL,
    recorded_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS research.learning_assessments (
    assessment_id text PRIMARY KEY,
    strategy_key text NOT NULL REFERENCES research.strategy_definitions(strategy_key),
    outcome_ids jsonb NOT NULL,
    sample_size integer NOT NULL CHECK (sample_size >= 0),
    win_rate double precision NOT NULL CHECK (win_rate BETWEEN 0 AND 1),
    average_net_return double precision NOT NULL,
    cumulative_net_pnl double precision NOT NULL,
    disposition text NOT NULL CHECK (
        disposition IN ('INSUFFICIENT_DATA', 'RETAIN', 'REVIEW', 'SUSPENSION_REVIEW')
    ),
    generated_at timestamptz NOT NULL,
    policy_version text NOT NULL
);
