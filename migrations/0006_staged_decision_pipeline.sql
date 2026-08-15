CREATE TABLE IF NOT EXISTS control.decision_events (
    decision_id text PRIMARY KEY,
    cycle_id text NOT NULL,
    market text NOT NULL CHECK (market IN ('nse', 'forex', 'crypto')),
    symbol text NOT NULL,
    timeframe text,
    stage text NOT NULL CHECK (stage IN (
        'global', 'position_management', 'data_readiness', 'tradeability',
        'strategy_eligibility', 'signal', 'scoring', 'llm_review',
        'portfolio_construction', 'entry_revalidation', 'risk', 'execution'
    )),
    status text NOT NULL CHECK (status IN ('passed', 'rejected', 'skipped', 'ordered', 'error')),
    reason_code text NOT NULL,
    occurred_at timestamptz NOT NULL,
    candidate_id text,
    strategy_key text,
    detail text NOT NULL DEFAULT '',
    metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS decision_events_cycle_idx
    ON control.decision_events (cycle_id, stage, status, reason_code);

CREATE INDEX IF NOT EXISTS decision_events_symbol_idx
    ON control.decision_events (market, symbol, occurred_at DESC);

CREATE OR REPLACE VIEW control.decision_funnel AS
SELECT
    cycle_id,
    market,
    stage,
    status,
    reason_code,
    count(*) AS decision_count
FROM control.decision_events
GROUP BY cycle_id, market, stage, status, reason_code;
