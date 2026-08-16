# Executable runtime

NanoDelta runs one supervised process with one isolated worker for each of NSE,
Forex and Crypto. Each worker has the same lifecycle and scheduling contract;
market-specific provider and strategy code remains behind the cycle callback.

## Lifecycle

`STARTING → RUNNING → DRAINING → STOPPED`

Every worker executes at most one cycle at a time. SIGTERM/SIGINT requests a
drain: no new cycle starts, the in-flight cycle may finish, and the process exits.
`NANODELTA_DRAIN_TIMEOUT_SECONDS` bounds the wait.

The runtime persists each market's instance ID, state, heartbeat, most recent
cycle timestamps and latest error in `control.runtime_instances`. Operators can
distinguish a stopped process from a stale or failing one without relying on logs.

## Run

The deployment Compose file starts `runtime` after migrations:

```bash
docker compose up -d db migrate runtime api web
docker compose logs -f runtime
docker compose stop -t 45 runtime
```

| Variable | Default | Purpose |
|---|---:|---|
| `NANODELTA_CYCLE_SECONDS` | 60 | Delay between market cycles |
| `NANODELTA_HEARTBEAT_SECONDS` | 10 | Durable heartbeat cadence |
| `NANODELTA_DRAIN_TIMEOUT_SECONDS` | 30 | Maximum graceful drain |
| `NANODELTA_INSTANCE_ID` | hostname | Runtime ownership identifier |
| `NANODELTA_REALTIME_ENABLED` | false | Compose provider streams into workers |

## Realtime mode

Realtime mode uses capability-specific routes, not one global provider flag:

- NSE quotes: TrueData primary, Dhan fallback.
- Forex quotes: OANDA (no second provider is configured).
- Crypto quotes: OKX primary, Poloniex fallback.

In realtime mode workers immediately request the next bounded stream slice
instead of applying the scheduled-cycle delay. This prevents the ordinary
`NANODELTA_CYCLE_SECONDS` interval from creating an intentional feed gap.

Every reconnect creates a new connection and restores its subscription. Provider
transports use bounded exponential backoff with jitter. The composition layer
rejects stale streams, records sequence gaps where a provider supplies a
sequence, fails over to the next provider, and requires three successful primary
probes after the recovery cooldown before returning to it.

Set these values through the deployment secret mounts/environment:

```dotenv
NANODELTA_REALTIME_ENABLED=true
NSE_DHAN_SECURITY_IDS_JSON={"RELIANCE":"1333"}
TRUEDATA_USERNAME=...
TRUEDATA_PASSWORD_PATH=/run/secrets/truedata_password
DHAN_CLIENT_ID=...
DHAN_ACCESS_TOKEN_PATH=/run/secrets/dhan_access_token
FOREX_SYMBOLS=EUR_USD,GBP_USD
OANDA_ACCOUNT_ID=...
OANDA_ACCESS_TOKEN_PATH=/run/secrets/oanda_access_token
OANDA_ENVIRONMENT=practice
CRYPTO_SYMBOLS=BTC_USDT,ETH_USDT
```

Ticks enter Bronze immediately. A one-minute candle remains in memory while it
is forming and is written to Bronze/Silver only after a tick opens the next UTC
minute. Therefore an incomplete realtime candle cannot enter Silver. The
workers persist market data and health only; no provider client has live-order
authority and this path remains paper-only.

## Safety and current boundary

The executable supports provider composition, but realtime mode remains explicit
opt-in. Known limitations are precise:

- OANDA has no configured realtime fallback.
- Dhan and ordinary ticker streams do not expose a reliable sequence number;
  gap detection there relies on staleness/time, not sequence continuity.
- Forming candles are memory-resident and are lost on process restart. They are
  deliberately not reconstructed into Silver from partial data.
- Provider-native connection retry counts are not yet persisted in
  `control.runtime_instances`; failover and detected gaps are held in the cycle
  snapshot.
- TrueData requires its optional proprietary SDK/package and a valid subscription.
- No credentialed realtime soak-session evidence is committed. Live tests are
  opt-in and require secret-file paths.
- This layer does not promote strategies, create broker orders, or prove a full
  market-session paper-trading run.
