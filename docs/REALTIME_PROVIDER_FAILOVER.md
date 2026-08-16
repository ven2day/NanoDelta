# Realtime provider failover

## Data path

```text
provider stream -> normalized quote -> Bronze
                              |
                              +-> forming 1m candle (memory only)
                                      |
                                      +-> next bucket observed -> settled Bronze/Silver
```

`RealtimeMarketCycle` is a `MarketWorker` callback. It consumes a bounded slice
so the existing supervisor retains heartbeat, failure isolation, drain and
shutdown ownership. The route comes from `ProviderRegistry` for
`REALTIME_QUOTES`; historical and order-book routes are unaffected.

## Failure policy

1. Provider transports reconnect and resubscribe with bounded exponential
   backoff and jitter.
2. No event before the staleness deadline is a provider failure.
3. The cycle moves to the next provider in the capability route.
4. After cooldown, the primary is probed without persisting the probe event.
5. Three consecutive successful probes are required before recovery.
6. If every provider fails, the worker records the cycle error and retries on
   its next supervised interval. It does not fabricate data or signals.

Sequence jumps are counted only when the provider payload supplies a sequence.
Out-of-order old events do not rewind the stored sequence. A gap records data
quality evidence but does not fabricate a REST repair because most providers do
not expose tick replay.

## Tests and safety

`tests/fixtures/providers/realtime_streams.json` contains provider-native Dhan,
TrueData, OANDA, OKX and Poloniex fixtures. Tests cover normalization, failover,
hysteresis recovery, sequence gaps, total failure and candle settlement.

Credentialed tests are skipped unless `NANODELTA_LIVE_PROVIDER_TESTS=1` and the
documented secret paths exist. Public OKX is also explicit opt-in. This subsystem
has no order API and cannot call a broker execution endpoint.
