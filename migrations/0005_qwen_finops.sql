CREATE TABLE IF NOT EXISTS control.llm_usage (
    record_id text PRIMARY KEY,
    provider_request_id text NOT NULL UNIQUE,
    provider text NOT NULL,
    model text NOT NULL,
    deployment_scope text NOT NULL,
    billing_mode text NOT NULL CHECK (billing_mode IN ('PAYG', 'SUBSCRIPTION')),
    market text CHECK (market IS NULL OR market IN ('nse', 'forex', 'crypto')),
    component text NOT NULL,
    reason text NOT NULL,
    input_tokens bigint NOT NULL CHECK (input_tokens >= 0),
    output_tokens bigint NOT NULL CHECK (output_tokens >= 0),
    cached_input_tokens bigint NOT NULL CHECK (cached_input_tokens >= 0),
    reasoning_tokens bigint NOT NULL CHECK (reasoning_tokens >= 0),
    marginal_cost_usd numeric(20, 10) NOT NULL CHECK (marginal_cost_usd >= 0),
    catalog_version text,
    occurred_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS llm_usage_daily_idx
    ON control.llm_usage (occurred_at, market, component, reason);

CREATE TABLE IF NOT EXISTS control.llm_finops_alerts (
    alert_id text PRIMARY KEY,
    alert_type text NOT NULL,
    threshold numeric(10, 6) NOT NULL,
    observed numeric(20, 6) NOT NULL,
    occurred_at timestamptz NOT NULL,
    acknowledged_at timestamptz,
    acknowledged_by text
);

CREATE TABLE IF NOT EXISTS control.llm_kill_switch (
    provider text PRIMARY KEY,
    active boolean NOT NULL,
    reason text,
    updated_at timestamptz NOT NULL,
    updated_by text NOT NULL
);

INSERT INTO control.llm_kill_switch(provider, active, reason, updated_at, updated_by)
VALUES ('qwen', false, NULL, now(), 'migration')
ON CONFLICT (provider) DO NOTHING;
