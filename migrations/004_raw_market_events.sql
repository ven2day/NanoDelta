-- Immutable Raw/Bronze provider payloads, isolated by market schema.

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
            'CREATE TABLE IF NOT EXISTS %I.raw_market_events ('
            'event_id text PRIMARY KEY, provider text NOT NULL, '
            'event_type text NOT NULL, symbol text NOT NULL, channel text NOT NULL, '
            'source_event_time timestamptz NOT NULL, received_at timestamptz NOT NULL, '
            'payload_hash text NOT NULL, schema_version text NOT NULL, '
            'payload jsonb NOT NULL)',
            target_schema
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON %I.raw_market_events '
            '(symbol, event_type, source_event_time DESC)',
            'ix_' || target_schema || '_raw_symbol_time', target_schema
        );
    END LOOP;
END $$;

INSERT INTO common.market_schema_migrations(migration_id, details)
VALUES (
    '004_raw_market_events',
    '{"layer":"RAW","append_only":true,"market_isolated":true}'::jsonb
)
ON CONFLICT (migration_id) DO NOTHING;
