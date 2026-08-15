# NanoDelta target architecture

## Current state versus target state

The repository currently contains a small working ETL kernel: provider payload adapters,
immutable Bronze records, canonical Silver candles, deterministic Gold features, local
idempotent storage, and tests. Everything after Gold in this document is a target contract
and must be implemented checkpoint by checkpoint.

## End-to-end flow

```text
NSE: Dhan + TrueData       Forex: OANDA       Crypto: OKX + Poloniex
          \                    |                       /
           +--------- Provider ingestion engines ----+
                                  |
                        Raw / Bronze events
                                  |
                validation + canonical normalization
                                  |
                     Canonical / Silver market data
                                  |
                settled-candle feature materialization
                                  |
                       Feature / Gold snapshots
                                  |
              strategy registry + candidate generation
                                  |
                  offline-approved strategy admission
                                  |
           TradingAgents research evidence (optional/cached)
                                  |
                deterministic qualification and risk
                                  |
                         final BUY/SELL decision
                                  |
                         paper execution engine
                                  |
                   fills -> positions -> outcomes
                                  |
                     monitoring API -> final UI
```

## Shared core ownership

The shared core owns contracts, not provider or market policy:

- identities: market, provider, symbol, timeframe, event time, correlation ID;
- Bronze, Silver, and Gold record schemas;
- data-quality result vocabulary;
- provider capability routing and fallback contracts;
- strategy definition, validation artifact, and approval contracts;
- agent evidence contracts;
- decision, paper execution, and outcome contracts;
- idempotency, lineage, clocks, and observability interfaces.

## Market ownership

| Responsibility | NSE | Forex | Crypto |
|---|---|---|---|
| Primary historical | Dhan | OANDA | OKX |
| Historical fallback | TrueData when licensed/capable | none initially | Poloniex |
| Primary realtime | TrueData | OANDA | OKX |
| Realtime fallback | Dhan | reconnect to OANDA | Poloniex |
| Canonical symbol | `RELIANCE` | `EUR_USD` | `BTC_USDT` |
| Calendar | NSE sessions/holidays | 24x5 UTC | 24x7 UTC |
| Execution | paper | paper | paper |

Fallback is capability-specific. A provider can be historical-primary and realtime-fallback;
there is no single global `primary_provider` flag.

## Process boundaries

Each engine reads only its declared input and writes only its declared output:

| Engine | Reads | Writes |
|---|---|---|
| Ingestion | provider APIs/streams | Bronze |
| Normalization | Bronze | Silver + quality failures |
| Feature | settled Silver | Gold |
| Strategy | Gold + approved registry | candidates |
| TradingAgents adapter | Gold + context + candidate | agent evidence |
| Qualification/risk | candidate + evidence + paper state | final decision |
| Paper execution | approved final decision | order/fill/position |
| Outcome | closed position + lineage | outcome/performance |
| API | all read models | JSON/SSE/WebSocket views |
| UI | API only | user commands through audited API |

## Isolation rules

- No cross-market database joins in runtime paths.
- Provider symbols are stored in Bronze only; Silver uses canonical symbols.
- Every primary/unique key contains or is isolated by market.
- A failure in one market worker cannot stop another market worker.
- Cross-market dashboards query read models; they do not combine runtime state objects.
- Secrets stay in environment/secret stores and are never written to Bronze.

