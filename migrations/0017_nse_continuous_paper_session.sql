CREATE TABLE IF NOT EXISTS control.paper_session_cycles (
    session_cycle_id text PRIMARY KEY,
    market text NOT NULL CHECK (market = 'nse'),
    feature_record_ids jsonb NOT NULL,
    event_time timestamptz NOT NULL,
    evaluated_at timestamptz NOT NULL,
    state text NOT NULL CHECK (state IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')),
    claim_token text,
    locked_until timestamptz,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    decision_cycle_id text,
    cycle_mode text CHECK (cycle_mode IN ('NORMAL', 'EXITS_ONLY')),
    candidate_count integer NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
    allocation_count integer NOT NULL DEFAULT 0 CHECK (allocation_count >= 0),
    risk_decision_count integer NOT NULL DEFAULT 0 CHECK (risk_decision_count >= 0),
    order_count integer NOT NULL DEFAULT 0 CHECK (order_count >= 0),
    exit_count integer NOT NULL DEFAULT 0 CHECK (exit_count >= 0),
    last_error_type text,
    finished_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS paper_session_cycles_state_idx
    ON control.paper_session_cycles (market, state, updated_at DESC);

CREATE INDEX IF NOT EXISTS paper_session_cycles_event_idx
    ON control.paper_session_cycles (market, event_time DESC);
