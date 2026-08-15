-- Versioned Decision-layer journal, isolated by market schema.

CREATE SCHEMA IF NOT EXISTS common;
CREATE SCHEMA IF NOT EXISTS nse;
CREATE SCHEMA IF NOT EXISTS forex;
CREATE SCHEMA IF NOT EXISTS crypto;

CREATE TABLE IF NOT EXISTS common.market_schema_migrations (
    migration_id text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now(),
    details jsonb NOT NULL DEFAULT '{}'::jsonb
);

DO $$
DECLARE
    target_schema text;
BEGIN
    FOREACH target_schema IN ARRAY ARRAY['nse', 'forex', 'crypto'] LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I.decision_records ('
            'decision_id text PRIMARY KEY, decision_version text NOT NULL, '
            'candidate_id text NOT NULL, feature_snapshot_id text, provider text NOT NULL, '
            'symbol text NOT NULL, timeframe text NOT NULL, side text NOT NULL, '
            'settled_candle_timestamp timestamptz NOT NULL, status text NOT NULL, '
            'final_action text NOT NULL, rejection_reasons jsonb NOT NULL, '
            'evidence jsonb NOT NULL, payload jsonb NOT NULL, updated_at timestamptz NOT NULL)',
            target_schema
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON %I.decision_records '
            '(symbol, timeframe, settled_candle_timestamp DESC)',
            'ix_' || target_schema || '_decision_symbol_time', target_schema
        );
    END LOOP;
END $$;

INSERT INTO common.market_schema_migrations(migration_id, details)
VALUES (
    '006_decision_records',
    '{"layer":"DECISION","versioned":true,"market_isolated":true}'::jsonb
)
ON CONFLICT (migration_id) DO NOTHING;
