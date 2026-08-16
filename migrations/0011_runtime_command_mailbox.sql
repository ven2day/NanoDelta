CREATE TABLE IF NOT EXISTS control.runtime_command_queue (
    command_id text PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    market text NOT NULL CHECK (market IN ('nse', 'forex', 'crypto')),
    command text NOT NULL CHECK (command IN ('start', 'stop', 'drain')),
    state text NOT NULL DEFAULT 'PENDING'
        CHECK (state IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    requested_at timestamptz NOT NULL,
    claimed_at timestamptz,
    completed_at timestamptz,
    instance_id text,
    last_error text
);

CREATE INDEX IF NOT EXISTS runtime_command_queue_pending_idx
    ON control.runtime_command_queue (state, requested_at);
