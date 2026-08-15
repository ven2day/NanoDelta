-- Versioned Feature/Gold snapshots, isolated by market schema.

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
            'CREATE TABLE IF NOT EXISTS %I.feature_snapshots ('
            'snapshot_id text PRIMARY KEY, provider text NOT NULL, symbol text NOT NULL, '
            'timeframe text NOT NULL, settled_candle_timestamp timestamptz NOT NULL, '
            'feature_version text NOT NULL, volume_type text NOT NULL, '
            'indicators jsonb NOT NULL, market_relative jsonb, payload jsonb NOT NULL, '
            'created_at timestamptz NOT NULL DEFAULT now())',
            target_schema
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON %I.feature_snapshots '
            '(symbol, timeframe, settled_candle_timestamp DESC)',
            'ix_' || target_schema || '_feature_symbol_time', target_schema
        );
    END LOOP;
END $$;

INSERT INTO common.market_schema_migrations(migration_id, details)
VALUES (
    '005_feature_snapshots',
    '{"layer":"FEATURE","versioned":true,"market_isolated":true}'::jsonb
)
ON CONFLICT (migration_id) DO NOTHING;
