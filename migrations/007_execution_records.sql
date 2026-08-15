-- Paper Execution-layer journal, isolated by market schema.

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
            'CREATE TABLE IF NOT EXISTS %I.execution_records ('
            'execution_id text PRIMARY KEY, execution_version text NOT NULL, decision_id text, '
            'intent_id text NOT NULL, order_id text, position_id text, provider text NOT NULL, '
            'mode text NOT NULL, status text NOT NULL, symbol text NOT NULL, side text NOT NULL, '
            'requested_quantity double precision NOT NULL, requested_price double precision NOT NULL, '
            'filled_quantity double precision NOT NULL, fill_price double precision NOT NULL, '
            'payload jsonb NOT NULL, updated_at timestamptz NOT NULL)',
            target_schema
        );
        EXECUTE format(
            'CREATE UNIQUE INDEX IF NOT EXISTS %I ON %I.execution_records (intent_id)',
            'ux_' || target_schema || '_execution_intent', target_schema
        );
    END LOOP;
END $$;

INSERT INTO common.market_schema_migrations(migration_id, details)
VALUES (
    '007_execution_records',
    '{"layer":"EXECUTION","paper_only":true,"market_isolated":true}'::jsonb
)
ON CONFLICT (migration_id) DO NOTHING;
