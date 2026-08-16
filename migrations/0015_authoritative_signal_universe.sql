CREATE TABLE IF NOT EXISTS control.signal_candidates (
    candidate_id text PRIMARY KEY,
    cycle_id text NOT NULL,
    market text NOT NULL CHECK (market IN ('nse', 'forex', 'crypto')),
    symbol text NOT NULL,
    timeframe text NOT NULL,
    strategy_key text NOT NULL,
    approval_id text NOT NULL,
    event_time timestamptz NOT NULL,
    action text NOT NULL CHECK (action IN ('BUY', 'SELL')),
    reference_price double precision NOT NULL CHECK (reference_price > 0),
    stop_price double precision NOT NULL,
    target_price double precision NOT NULL,
    confidence double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    gold_snapshot_ids jsonb NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS signal_candidates_cycle_idx
    ON control.signal_candidates (cycle_id, market, event_time DESC);

CREATE INDEX IF NOT EXISTS signal_candidates_symbol_idx
    ON control.signal_candidates (market, symbol, timeframe, event_time DESC);

CREATE TABLE IF NOT EXISTS control.market_universe (
    market text NOT NULL CHECK (market IN ('nse', 'forex', 'crypto')),
    symbol text NOT NULL,
    provider text NOT NULL,
    provider_symbol text NOT NULL,
    timeframes jsonb NOT NULL,
    trade_horizon text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    configured_at timestamptz NOT NULL,
    PRIMARY KEY (market, symbol)
);

CREATE INDEX IF NOT EXISTS market_universe_enabled_idx
    ON control.market_universe (market, enabled, symbol);
