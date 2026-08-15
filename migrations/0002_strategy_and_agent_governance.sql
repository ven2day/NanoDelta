CREATE TABLE IF NOT EXISTS research.strategy_definitions (
    strategy_key text PRIMARY KEY,
    market text NOT NULL CHECK (market IN ('nse', 'forex', 'crypto')),
    strategy_id text NOT NULL,
    strategy_version text NOT NULL,
    timeframe text NOT NULL,
    trade_horizon text NOT NULL,
    feature_set_version integer NOT NULL CHECK (feature_set_version > 0),
    family text NOT NULL,
    parameters jsonb NOT NULL,
    implementation_ref text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (
        market, strategy_id, strategy_version, timeframe, trade_horizon, feature_set_version
    )
);

CREATE TABLE IF NOT EXISTS research.validation_runs (
    validation_run_id text PRIMARY KEY,
    strategy_key text NOT NULL REFERENCES research.strategy_definitions(strategy_key),
    evaluated_at timestamptz NOT NULL,
    passed boolean NOT NULL,
    metrics jsonb NOT NULL,
    policy jsonb NOT NULL,
    rejection_reasons jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS research.strategy_approvals (
    approval_id text PRIMARY KEY,
    strategy_key text NOT NULL REFERENCES research.strategy_definitions(strategy_key),
    validation_run_id text NOT NULL REFERENCES research.validation_runs(validation_run_id),
    state text NOT NULL CHECK (state IN ('APPROVED', 'REVOKED')),
    approved_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    approved_by text NOT NULL,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (expires_at > approved_at)
);

CREATE INDEX IF NOT EXISTS strategy_approvals_runtime_idx
    ON research.strategy_approvals (strategy_key, state, expires_at DESC);

CREATE TABLE IF NOT EXISTS research.agent_evidence (
    evidence_id text PRIMARY KEY,
    cache_key text NOT NULL UNIQUE,
    input_fingerprint text NOT NULL,
    candidate_id text NOT NULL,
    approval_id text NOT NULL REFERENCES research.strategy_approvals(approval_id),
    framework text NOT NULL,
    framework_version text NOT NULL,
    model_config jsonb NOT NULL,
    started_at timestamptz NOT NULL,
    completed_at timestamptz NOT NULL,
    action text NOT NULL CHECK (action IN ('BUY', 'SELL', 'ABSTAIN')),
    confidence double precision CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    roles jsonb NOT NULL,
    raw_decision text NOT NULL,
    error text,
    created_at timestamptz NOT NULL DEFAULT now()
);
