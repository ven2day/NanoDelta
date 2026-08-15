# Strategy governance and TradingAgents integration

## Strategy lifecycle

Strategies are versioned specifications, not arbitrary functions enabled directly in runtime.
The exact-identity registry and deterministic validation gates are implemented in
`nanodelta.strategies`; strategy-family signal evaluators are introduced separately.

```text
idea -> implementation -> backtest -> walk-forward validation -> cost/stress tests
     -> approval artifact -> runtime registry -> candidate -> final decision
```

Registry identity is exact:

```text
(market, strategy_id, strategy_version, timeframe, trade_horizon, feature_set_version)
```

A runtime candidate is admitted only when a current approval exists for that exact identity.
No live fitting, validation, or automatic approval is allowed.

## Initial strategy families

The platform may implement strategies incrementally, not all at once:

- EMA9 + RSI14 crossover/confirmation;
- SuperTrend `(ATR 14, multiplier 3)` with ADX confirmation;
- VWAP pullback for markets/sessions where VWAP semantics are valid;
- opening-range breakout for NSE only;
- trend pullback and momentum continuation;
- range mean-reversion with regime guard;
- Crypto order-book imbalance after order-book quality is proven.

Each market owns enablement and parameters. A strategy existing in shared code does not make it
eligible for all three markets.

## Selection process

1. Load settled Gold snapshot.
2. Resolve market/timeframe/horizon eligible strategies from the approval registry.
3. Evaluate all eligible strategies deterministically.
4. Consolidate duplicates by symbol, direction, horizon, and evidence window.
5. Reject conflicting or stale evidence.
6. Rank candidates by validated expectancy, cost-adjusted quality, freshness, and diversification.
7. Send only bounded high-quality candidates for optional agent research.
8. Apply deterministic qualification and risk.
9. Produce BUY, SELL, or abstain with a complete audit trail.

Walk-forward validation is a hard admission gate for an automated strategy. TradingAgents cannot
replace it because an LLM discussion is non-deterministic and is not out-of-sample evidence.

## TradingAgents source and role

[TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) is a LangGraph
multi-agent research framework. Its documented roles include fundamental, sentiment, news, and
technical analysts; opposing researchers; a trader; risk-management agents; and a portfolio
manager. It also provides a persistent decision log and optional SQLite checkpoints.

NanoDelta integrates it through `nanodelta.agents.tradingagents` instead of merging its
repository wholesale.
TradingAgents is an optional evidence processor with these boundaries:

```text
Gold snapshot + candidate + approved context
                  |
          TradingAgents adapter
                  |
 structured evidence + recommendation + confidence + citations
                  |
      NanoDelta deterministic validation and risk
                  |
          final BUY / SELL / abstain
```

TradingAgents must never:

- read provider credentials;
- fetch alternative prices that override NanoDelta Silver/Gold;
- write Bronze, Silver, Gold, orders, fills, or positions;
- approve an unvalidated strategy;
- choose position size or relax limits;
- call a broker/exchange;
- turn an LLM response directly into an order.

## Role mapping

| TradingAgents concept | NanoDelta use |
|---|---|
| Technical Analyst | reads exact Gold snapshot; returns structured observations |
| News Analyst | returns timestamped/cited context |
| Sentiment Analyst | returns bounded supporting context |
| Fundamentals Analyst | NSE equities where identifiers/data exist; otherwise disabled |
| Opposing researchers | structured arguments for and against the candidate |
| Trader Agent | recommendation only, renamed/treated as `candidate_reviewer` |
| Risk agents | advisory evidence only; deterministic NanoDelta risk remains authoritative |
| Portfolio Manager | recommendation only; cannot approve execution |

Use BUY and SELL terminology in NanoDelta contracts even if upstream code uses other terms.

## Agent persistence

Persist immutable structured records, not only Markdown:

- input snapshot IDs and timestamps;
- candidate and strategy approval IDs;
- agent/framework version and graph configuration;
- LLM provider/model, prompt version, temperature, and token/cost data;
- per-role evidence, citations, recommendation, and confidence;
- timeout/error/fallback state;
- final NanoDelta decision and whether agent evidence influenced it.

Cache by exact input fingerprint. Expired or failed agent analysis must result in deterministic
fallback or abstention—not a hidden retry storm. Agent results are not included in Gold because
Gold must remain deterministic and reproducible.

The implemented adapter accepts only a candidate linked to a current exact-identity approval,
normalizes upstream output to BUY, SELL, or ABSTAIN, records selected role reports, and converts
backend failure to explicit ABSTAIN evidence. It deliberately exposes no order or broker
interface. Production configuration must record an exact upstream release and commit.

## Licensing and dependency policy

TradingAgents is published under Apache-2.0. Prefer a pinned external dependency, service, or
adapter. If code is copied or modified, retain required notices and attribution. Record the exact
upstream commit/version because its agent graph and data-access behavior can change.
