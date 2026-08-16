CREATE TABLE IF NOT EXISTS control.realtime_feed_state (
    market text PRIMARY KEY CHECK (market IN ('nse', 'forex', 'crypto')),
    active_provider text NOT NULL,
    state text NOT NULL CHECK (state IN ('HEALTHY', 'DEGRADED', 'FAILED_OVER')),
    connected_at timestamptz NOT NULL,
    last_event_at timestamptz,
    gap_count bigint NOT NULL DEFAULT 0 CHECK (gap_count >= 0),
    failover_count bigint NOT NULL DEFAULT 0 CHECK (failover_count >= 0),
    last_error text,
    failed_over_at timestamptz,
    fallback_available boolean NOT NULL,
    status_detail text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS control.realtime_sequence_state (
    market text NOT NULL CHECK (market IN ('nse', 'forex', 'crypto')),
    provider text NOT NULL,
    symbol text NOT NULL,
    last_sequence bigint NOT NULL,
    gap_count bigint NOT NULL DEFAULT 0 CHECK (gap_count >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (market, provider, symbol)
);
