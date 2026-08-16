CREATE TABLE IF NOT EXISTS paper.realization_events (
    event_id text PRIMARY KEY,
    fill_id text NOT NULL UNIQUE REFERENCES paper.fills(fill_id),
    position_id text NOT NULL REFERENCES paper.positions(position_id),
    market text NOT NULL CHECK (market IN ('nse', 'forex', 'crypto')),
    account_id text NOT NULL,
    symbol text NOT NULL,
    gross_pnl_delta double precision NOT NULL,
    fee double precision NOT NULL CHECK (fee >= 0),
    net_pnl double precision NOT NULL,
    realized_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS paper_realization_events_account_day_idx
    ON paper.realization_events (market, account_id, realized_at);
