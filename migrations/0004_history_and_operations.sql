CREATE TABLE IF NOT EXISTS control.history_runs (
    run_id text PRIMARY KEY,
    market text NOT NULL CHECK (market IN ('nse', 'forex', 'crypto')),
    symbol text NOT NULL,
    timeframe text NOT NULL,
    state text NOT NULL CHECK (state IN ('RUNNING', 'SUCCEEDED', 'FAILED')),
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    provider text,
    rows_received bigint NOT NULL DEFAULT 0,
    bronze_created bigint NOT NULL DEFAULT 0,
    silver_created bigint NOT NULL DEFAULT 0,
    error_message text
);

CREATE INDEX IF NOT EXISTS history_runs_status_idx
    ON control.history_runs (market, symbol, timeframe, started_at DESC);

CREATE TABLE IF NOT EXISTS control.runtime_workers (
    market text PRIMARY KEY CHECK (market IN ('nse', 'forex', 'crypto')),
    state text NOT NULL CHECK (state IN ('STOPPED', 'RUNNING', 'DRAINING')),
    last_heartbeat timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO control.runtime_workers (market, state)
VALUES ('nse', 'STOPPED'), ('forex', 'STOPPED'), ('crypto', 'STOPPED')
ON CONFLICT (market) DO NOTHING;

CREATE TABLE IF NOT EXISTS control.operational_audit (
    audit_id text PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    market text NOT NULL CHECK (market IN ('nse', 'forex', 'crypto')),
    command text NOT NULL CHECK (command IN ('start', 'stop', 'drain', 'repair')),
    actor_id text NOT NULL,
    previous_state text NOT NULL CHECK (previous_state IN ('STOPPED', 'RUNNING', 'DRAINING')),
    resulting_state text NOT NULL CHECK (resulting_state IN ('STOPPED', 'RUNNING', 'DRAINING')),
    requested_at timestamptz NOT NULL,
    detail text NOT NULL
);

CREATE TABLE IF NOT EXISTS control.history_repair_queue (
    repair_id text PRIMARY KEY,
    idempotency_key text NOT NULL,
    market text NOT NULL CHECK (market IN ('nse', 'forex', 'crypto')),
    symbol text NOT NULL,
    timeframe text NOT NULL,
    gap_start timestamptz NOT NULL,
    gap_end timestamptz NOT NULL,
    state text NOT NULL CHECK (state IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (market, symbol, timeframe, gap_start, gap_end)
);
