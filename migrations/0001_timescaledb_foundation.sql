CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE SCHEMA IF NOT EXISTS control;
CREATE SCHEMA IF NOT EXISTS research;
CREATE SCHEMA IF NOT EXISTS paper;

CREATE TABLE IF NOT EXISTS control.schema_migrations (
    version text PRIMARY KEY,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS control.ingestion_runs (
    run_id uuid PRIMARY KEY,
    market text NOT NULL CHECK (market IN ('nse', 'forex', 'crypto')),
    provider text NOT NULL,
    dataset text NOT NULL,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    state text NOT NULL CHECK (state IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
    rows_received bigint NOT NULL DEFAULT 0,
    bronze_created bigint NOT NULL DEFAULT 0,
    silver_created bigint NOT NULL DEFAULT 0,
    error_message text
);

CREATE TABLE IF NOT EXISTS control.provider_watermarks (
    market text NOT NULL CHECK (market IN ('nse', 'forex', 'crypto')),
    provider text NOT NULL,
    dataset text NOT NULL,
    symbol text NOT NULL,
    timeframe text NOT NULL,
    cursor_value text,
    event_time_watermark timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (market, provider, dataset, symbol, timeframe)
);

CREATE TABLE IF NOT EXISTS control.data_quality_issues (
    issue_id uuid PRIMARY KEY,
    market text NOT NULL CHECK (market IN ('nse', 'forex', 'crypto')),
    raw_record_id text NOT NULL,
    rule_code text NOT NULL,
    detail text NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz
);

DO $foundation$
DECLARE
    market_name text;
BEGIN
    FOREACH market_name IN ARRAY ARRAY['nse', 'forex', 'crypto']
    LOOP
        EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', market_name || '_bronze');
        EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', market_name || '_silver');
        EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', market_name || '_gold');

        EXECUTE format($sql$
            CREATE TABLE IF NOT EXISTS %I.raw_events (
                record_id text PRIMARY KEY,
                provider text NOT NULL,
                event_type text NOT NULL,
                provider_symbol text NOT NULL,
                source_event_time timestamptz,
                received_at timestamptz NOT NULL,
                schema_version integer NOT NULL,
                payload_hash text NOT NULL,
                payload jsonb NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            )
        $sql$, market_name || '_bronze');

        EXECUTE format($sql$
            CREATE TABLE IF NOT EXISTS %I.candles (
                symbol text NOT NULL,
                timeframe text NOT NULL,
                open_time timestamptz NOT NULL,
                provider text NOT NULL,
                raw_record_id text NOT NULL,
                open double precision NOT NULL,
                high double precision NOT NULL,
                low double precision NOT NULL,
                close double precision NOT NULL,
                volume double precision NOT NULL CHECK (volume >= 0),
                is_settled boolean NOT NULL DEFAULT true,
                schema_version integer NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (symbol, timeframe, open_time)
            )
        $sql$, market_name || '_silver');

        EXECUTE format($sql$
            CREATE TABLE IF NOT EXISTS %I.feature_snapshots (
                record_id text NOT NULL,
                candle_record_id text NOT NULL,
                symbol text NOT NULL,
                timeframe text NOT NULL,
                event_time timestamptz NOT NULL,
                feature_version integer NOT NULL,
                features jsonb NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (record_id, event_time)
            )
        $sql$, market_name || '_gold');

        PERFORM create_hypertable(
            format('%I.%I', market_name || '_silver', 'candles')::regclass,
            'open_time',
            if_not_exists => TRUE,
            migrate_data => TRUE
        );
        PERFORM create_hypertable(
            format('%I.%I', market_name || '_gold', 'feature_snapshots')::regclass,
            'event_time',
            if_not_exists => TRUE,
            migrate_data => TRUE
        );

        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON %I.candles (symbol, timeframe, open_time DESC)',
            market_name || '_candles_lookup_idx', market_name || '_silver'
        );
    END LOOP;
END
$foundation$;
