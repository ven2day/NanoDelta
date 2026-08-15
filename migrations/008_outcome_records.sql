-- Closed-trade Outcome and attribution records, isolated by market schema.

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
            'CREATE TABLE IF NOT EXISTS %I.outcome_records ('
            'outcome_id text PRIMARY KEY, outcome_version text NOT NULL, trade_id text NOT NULL, '
            'decision_id text, entry_execution_id text, exit_execution_id text, '
            'feature_snapshot_id text, provider text NOT NULL, symbol text NOT NULL, '
            'timeframe text NOT NULL, side text NOT NULL, strategy text NOT NULL, '
            'closed_at timestamptz NOT NULL, net_pnl double precision NOT NULL, '
            'return_pct double precision NOT NULL, is_winner boolean NOT NULL, '
            'learning_eligible boolean NOT NULL, payload jsonb NOT NULL)',
            target_schema
        );
        EXECUTE format(
            'CREATE UNIQUE INDEX IF NOT EXISTS %I ON %I.outcome_records (trade_id)',
            'ux_' || target_schema || '_outcome_trade', target_schema
        );
    END LOOP;
END $$;

INSERT INTO common.market_schema_migrations(migration_id, details)
VALUES (
    '008_outcome_records',
    '{"layer":"OUTCOME","closed_trades_only":true,"offline_learning":true,"market_isolated":true}'::jsonb
)
ON CONFLICT (migration_id) DO NOTHING;
