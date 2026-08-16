CREATE TABLE IF NOT EXISTS research.nse_validation_campaigns (
    campaign_id text PRIMARY KEY,
    evaluated_at timestamptz NOT NULL,
    requested_start timestamptz NOT NULL,
    requested_end timestamptz NOT NULL,
    minimum_history_days integer NOT NULL CHECK (minimum_history_days >= 730),
    source_provider text NOT NULL CHECK (source_provider = 'dhan'),
    symbols jsonb NOT NULL,
    required_timeframes jsonb NOT NULL,
    validation_config jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (requested_start < requested_end)
);

CREATE TABLE IF NOT EXISTS research.nse_validation_readiness (
    readiness_id text PRIMARY KEY,
    campaign_id text NOT NULL REFERENCES research.nse_validation_campaigns(campaign_id),
    symbol text NOT NULL,
    timeframe text NOT NULL CHECK (timeframe IN ('5m', '15m', '30m', '1h')),
    first_open timestamptz,
    last_open timestamptz,
    settled_count bigint NOT NULL CHECK (settled_count >= 0),
    minimum_settled_count bigint NOT NULL CHECK (minimum_settled_count >= 0),
    history_days integer NOT NULL CHECK (history_days >= 0),
    ready boolean NOT NULL,
    reasons jsonb NOT NULL,
    source_fingerprint text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, symbol, timeframe)
);

CREATE TABLE IF NOT EXISTS research.nse_strategy_evidence (
    evidence_id text PRIMARY KEY,
    campaign_id text NOT NULL REFERENCES research.nse_validation_campaigns(campaign_id),
    validation_run_id text NOT NULL REFERENCES research.validation_runs(validation_run_id),
    strategy_key text NOT NULL REFERENCES research.strategy_definitions(strategy_key),
    research_state text NOT NULL CHECK (research_state IN ('RESEARCH', 'FAILED')),
    data_fingerprint text NOT NULL,
    walk_forward_windows jsonb NOT NULL,
    cost_model jsonb NOT NULL,
    stressed_net_expectancy double precision NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, strategy_key)
);

CREATE TABLE IF NOT EXISTS research.nse_strategy_promotions (
    promotion_id text PRIMARY KEY,
    evidence_id text NOT NULL UNIQUE REFERENCES research.nse_strategy_evidence(evidence_id),
    approval_id text NOT NULL UNIQUE REFERENCES research.strategy_approvals(approval_id),
    reviewed_by text NOT NULL,
    review_reason text NOT NULL,
    promoted_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS nse_validation_campaign_time_idx
    ON research.nse_validation_campaigns (evaluated_at DESC);
CREATE INDEX IF NOT EXISTS nse_strategy_evidence_key_idx
    ON research.nse_strategy_evidence (strategy_key, created_at DESC);

CREATE OR REPLACE FUNCTION research.require_nse_technical_validation_evidence()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.state = 'APPROVED' AND EXISTS (
        SELECT 1
        FROM research.strategy_definitions d
        WHERE d.strategy_key = NEW.strategy_key
          AND d.market = 'nse'
          AND d.strategy_id IN ('vwap_pullback', 'ema_rsi_continuation', 'supertrend_adx')
    ) AND NOT EXISTS (
        SELECT 1
        FROM research.nse_strategy_evidence e
        JOIN research.validation_runs v ON v.validation_run_id = e.validation_run_id
        WHERE e.strategy_key = NEW.strategy_key
          AND e.validation_run_id = NEW.validation_run_id
          AND e.research_state = 'RESEARCH'
          AND v.passed = true
    ) THEN
        RAISE EXCEPTION 'passing NSE validation evidence is required for paper approval';
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS require_nse_technical_validation_evidence
    ON research.strategy_approvals;
CREATE TRIGGER require_nse_technical_validation_evidence
BEFORE INSERT OR UPDATE OF state, validation_run_id, strategy_key
ON research.strategy_approvals
FOR EACH ROW
EXECUTE FUNCTION research.require_nse_technical_validation_evidence();

CREATE OR REPLACE VIEW research.nse_strategy_validation_read AS
SELECT
    d.strategy_key,
    d.strategy_id,
    d.strategy_version,
    d.timeframe,
    d.trade_horizon,
    d.feature_set_version,
    d.family,
    d.parameters,
    e.evidence_id,
    e.campaign_id,
    e.research_state,
    v.validation_run_id,
    v.evaluated_at,
    v.passed,
    v.metrics,
    v.policy,
    v.rejection_reasons,
    a.approval_id,
    a.state AS approval_state,
    a.approved_at,
    a.expires_at,
    CASE
        WHEN a.state = 'APPROVED' AND a.approved_at <= now() AND a.expires_at > now()
            THEN 'PAPER_APPROVED'
        WHEN e.research_state = 'FAILED' THEN 'FAILED'
        ELSE 'RESEARCH'
    END AS lifecycle_state
FROM research.strategy_definitions d
LEFT JOIN LATERAL (
    SELECT evidence_id, campaign_id, validation_run_id, research_state
    FROM research.nse_strategy_evidence
    WHERE strategy_key = d.strategy_key
    ORDER BY created_at DESC
    LIMIT 1
) e ON true
LEFT JOIN research.validation_runs v ON v.validation_run_id = e.validation_run_id
LEFT JOIN LATERAL (
    SELECT approval.approval_id, approval.state, approval.approved_at, approval.expires_at
    FROM research.strategy_approvals approval
    JOIN research.nse_strategy_promotions promotion
      ON promotion.approval_id = approval.approval_id
    WHERE approval.strategy_key = d.strategy_key
    ORDER BY (
        approval.state = 'APPROVED'
        AND approval.approved_at <= now()
        AND approval.expires_at > now()
    ) DESC, approval.approved_at DESC
    LIMIT 1
) a ON true
WHERE d.market = 'nse';

CREATE OR REPLACE VIEW research.nse_backtest_read AS
SELECT
    e.evidence_id,
    e.campaign_id,
    e.strategy_key,
    d.strategy_id,
    d.strategy_version,
    d.timeframe,
    c.evaluated_at,
    c.requested_start,
    c.requested_end,
    c.minimum_history_days,
    c.source_provider,
    e.research_state,
    v.passed,
    v.metrics,
    v.policy,
    v.rejection_reasons,
    e.walk_forward_windows,
    e.cost_model,
    e.stressed_net_expectancy,
    e.data_fingerprint
FROM research.nse_strategy_evidence e
JOIN research.nse_validation_campaigns c ON c.campaign_id = e.campaign_id
JOIN research.validation_runs v ON v.validation_run_id = e.validation_run_id
JOIN research.strategy_definitions d ON d.strategy_key = e.strategy_key;
