# ETL and market-data engines

## Bronze ingestion engine

Bronze is append-only and preserves the provider payload after secret redaction. Each record
contains a deterministic record ID, market, provider, event type, provider symbol, provider
event time when available, received time, schema version, payload, and payload hash.

The engine must:

1. resolve the provider capability;
2. acquire the provider rate-limit permit;
3. fetch or receive data;
4. redact secrets and create a deterministic identity;
5. persist Bronze before attempting normalization;
6. update the watermark only after the durable write commits;
7. record metrics without blocking ingestion.

Invalid provider payloads remain in Bronze. Retries are idempotent.

## Silver normalization engine

Silver maps provider fields into canonical UTC records. Candle grain is:

```text
(market, canonical_symbol, timeframe, open_time)
```

Required checks include:

- provider belongs to the requested market;
- symbol mapping exists and is deterministic;
- timestamps are timezone-aware and aligned to the timeframe;
- numeric values are finite;
- `low <= open/close <= high`;
- volume is non-negative and its unit is known;
- candles are settled/complete;
- duplicate keys resolve idempotently;
- source-to-canonical lineage is preserved.

Rejected records produce a quality issue linked to the Bronze record. They do not silently
disappear and do not enter Gold.

## Gold feature engine

Gold features are calculated only from ordered, settled Silver records. A feature snapshot
must include its candle identity, feature-set name/version, calculation time, and exact input
window. The first feature set may include returns, range/body percentages, gaps, volatility,
volume change, EMA, RSI, ATR, ADX, SuperTrend, and VWAP where market semantics support them.

Feature code is shared where the formula is truly identical. Session-sensitive calculations
such as VWAP remain market-aware. Gold never contains a BUY/SELL decision.

## Historical backfill

For every active symbol, target at least 730 days where the provider supports it. Required
grains are 5m, 15m, 30m, 1h, 4h, and 1d. Prefer downloading the finest economical source
grain once and deriving higher grains when session alignment is provably correct.

Backfill priority:

1. active symbol with a recent gap;
2. symbol preventing readiness;
3. newest missing window;
4. older/deep history.

Jobs are resumable, bounded-concurrency, rate-limit-aware, and provider-page-aware. No worker
claims progress percentages unless total provider pages are known.

## Realtime engines

- NSE: TrueData realtime-primary; Dhan realtime-fallback.
- Forex: OANDA pricing/candles with reconnect and subscription restoration.
- Crypto: OKX realtime/order-book primary; Poloniex fallback where its capability is adequate.

Realtime ticks may update an in-memory forming bar, but only a settled bar is committed to
Silver and allowed to trigger Gold. Quote events may update open paper positions independently;
they do not rerun the full strategy pipeline.

## Scheduling

- A lightweight wake-up checks whether any settled-candle key changed.
- No-change cycles exit immediately.
- Changed grains trigger only affected feature and strategy partitions.
- Historical repair runs in the background and yields to realtime ingestion.
- Per-market workers expose start, stop, drain, health, lag, and last-success state.

