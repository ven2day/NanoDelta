CREATE TABLE IF NOT EXISTS control.runtime_instances (
    market text PRIMARY KEY CHECK (market IN ('nse', 'forex', 'crypto')),
    instance_id text NOT NULL,
    state text NOT NULL CHECK (state IN ('STARTING', 'RUNNING', 'DRAINING', 'STOPPED', 'FAILED')),
    last_heartbeat timestamptz NOT NULL,
    last_cycle_started timestamptz,
    last_cycle_finished timestamptz,
    last_error text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS runtime_instances_heartbeat_idx
    ON control.runtime_instances (state, last_heartbeat);
