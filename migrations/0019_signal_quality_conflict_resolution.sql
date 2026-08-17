ALTER TABLE control.decision_events DROP CONSTRAINT decision_events_stage_check;

ALTER TABLE control.decision_events ADD CONSTRAINT decision_events_stage_check CHECK (stage IN (
    'global', 'position_management', 'data_readiness', 'tradeability',
    'strategy_eligibility', 'signal', 'scoring', 'signal_quality', 'llm_review',
    'portfolio_construction', 'entry_revalidation', 'risk', 'execution'
));
