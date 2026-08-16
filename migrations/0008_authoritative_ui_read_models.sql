CREATE TABLE IF NOT EXISTS control.alert_events (
    alert_id text PRIMARY KEY,
    market text CHECK (market IS NULL OR market IN ('nse', 'forex', 'crypto')),
    severity text NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'CRITICAL')),
    component text NOT NULL,
    reason_code text NOT NULL,
    detail text NOT NULL,
    state text NOT NULL CHECK (state IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED')),
    occurred_at timestamptz NOT NULL,
    acknowledged_at timestamptz,
    resolved_at timestamptz
);

CREATE INDEX IF NOT EXISTS alert_events_active_idx
    ON control.alert_events (state, severity, occurred_at DESC);

CREATE TABLE IF NOT EXISTS control.system_settings (
    setting_key text NOT NULL,
    market text CHECK (market IS NULL OR market IN ('nse', 'forex', 'crypto')),
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL,
    updated_by text NOT NULL,
    UNIQUE NULLS NOT DISTINCT (setting_key, market)
);

CREATE TABLE IF NOT EXISTS control.report_runs (
    report_id text PRIMARY KEY,
    market text CHECK (market IS NULL OR market IN ('nse', 'forex', 'crypto')),
    report_type text NOT NULL,
    state text NOT NULL CHECK (state IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    parameters jsonb NOT NULL,
    artifact_uri text,
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    requested_by text NOT NULL
);
