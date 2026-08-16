# Realtime transport lifecycle and durable continuity

`RealtimeMarketCycle` owns one subscribed async iterator per active provider. Reaching the bounded
event budget yields control to the supervisor without closing that iterator, so the next slice
continues on the same connection and subscription. Provider exceptions, stale reads, clean remote
stream termination, failover and `aclose()` remove and close the iterator. The next attempt calls
the provider client's `stream(symbols, channel)` again, restoring the complete subscription.

Migration `0014_realtime_feed_state.sql` stores the active provider, feed health, failover and gap
counters, last error/event, fallback capability, and per-provider/symbol sequence watermark. Inject
`PostgresFeedStateStore` into each cycle during runtime composition. A restarted cycle hydrates this
state before consuming data, so a sequence gap or failed-over provider is not forgotten.

Shutdown ownership must call `await cycle.aclose()` after draining and before destroying the event
loop. The method is idempotent. Transport failures also close immediately, so a failed iterator is
never reused.

NSE has TrueData → Dhan routing and Crypto has OKX → Poloniex routing. Forex has only OANDA in the
current provider registry. Its durable state therefore reports `fallback_available=false` and
`NO_REALTIME_FALLBACK_CONFIGURED`; an OANDA failure becomes `DEGRADED` with operator action required.
No unsupported Forex provider is fabricated by this checkpoint.
