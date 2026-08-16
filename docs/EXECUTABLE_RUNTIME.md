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

The runtime is an explicit Compose profile because startup intentionally fails
until all three market configurations and secret files are present:

```bash
docker compose --profile market-runtime up -d
docker compose logs -f runtime
docker compose stop -t 45 runtime
```

| Variable | Default | Purpose |
|---|---:|---|
| `NANODELTA_HEARTBEAT_SECONDS` | 10 | Durable heartbeat cadence |
| `NANODELTA_DRAIN_TIMEOUT_SECONDS` | 30 | Maximum graceful drain |
| `NANODELTA_INSTANCE_ID` | hostname | Runtime ownership identifier |
| `NANODELTA_RUNTIME_MODE` | required | Must equal `realtime-paper` |

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
NANODELTA_RUNTIME_MODE=realtime-paper
NSE_DHAN_SECURITY_IDS_JSON={"RELIANCE":"1333"}
NANODELTA_NSE_REALTIME_TIMEFRAMES=1m,5m,15m
NANODELTA_NSE_HOLIDAYS=<comma-separated official ISO dates>
NANODELTA_NSE_HOLIDAY_CALENDAR_YEAR=2026
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

Ticks enter Bronze immediately. NSE builds configured UTC-aligned 1m, 5m and
15m candles in parallel. Each candle remains in memory while it is forming and
is written to Bronze/Silver only after a tick opens the next interval. Therefore
an incomplete realtime candle cannot enter Silver. Previous-candle state is
isolated by symbol and timeframe so Gold never mixes 1m/5m/15m records. Two
consecutive settled candles materialize versioned Gold features. Those features
enter exact strategy-approval admission, portfolio allocation, risk, and the
durable PostgreSQL paper executor. No provider client has live-order authority
and this path remains paper-only.

At startup the runtime reconciles the configured NSE symbols into
`control.market_universe`. Every generated BUY or SELL is written to
`control.signal_candidates` before scoring, portfolio, risk or execution can
reject it. Candidate lineage, proposed entry/stop/target and full deterministic
attribution therefore remain queryable even when no paper order is created.

NSE entry preconditions use the normal equity session (09:15–15:30 IST). Set the
holiday list and its calendar year from the official NSE Capital Market circular.
The session API reports `holiday_calendar_complete=false` when that annual input
has not been configured; it never labels the calendar as verified implicitly.
The normal-session timing source is NSE's official
[Market Timings](https://www.nseindia.com/static/market-data/market-timings) page.

## Safety and current boundary

The executable supports provider composition, but the Compose profile remains
explicit opt-in. It rejects an empty/idle configuration rather than publishing
misleading healthy heartbeats. Known limitations are precise:

- OANDA has no configured realtime fallback.
- Dhan and ordinary ticker streams do not expose a reliable sequence number;
  gap detection there relies on staleness/time, not sequence continuity.
- Forming candles at every configured timeframe are memory-resident and are lost on process restart. They are
  deliberately not reconstructed into Silver from partial data.
- Provider-native connection retry counts are not yet persisted in
  `control.runtime_instances`; failover and detected gaps are held in the cycle
  snapshot.
- TrueData requires its optional proprietary SDK/package and a valid subscription.
- No credentialed realtime soak-session evidence is committed. Live tests are
  opt-in and require secret-file paths.
- Built-in strategy definitions are registered but never auto-approved. Paper
  orders require a validation-backed, current approval for the exact definition;
  absent that approval, the durable ledger records the rejection.
- No live-order client or live-order authority exists.
# Runtime control plane

The API and market runtime are separate processes. `POST /api/{market}/runtime/start`,
`stop`, and `drain` never pretend to call an in-process worker. The API commits the
authenticated audit record and a command to `control.runtime_command_queue` in one
PostgreSQL transaction. The runtime claims commands with `FOR UPDATE SKIP LOCKED`,
applies them to the market worker, and records `SUCCEEDED` or `FAILED`.

Inspect a command with:

```text
GET /api/{market}/runtime-commands/{idempotency_key}
```

The market health endpoint reports the applied worker state, not the requested state.
Consequently a successful control POST means **durably queued**, not already applied.
The runtime container must remain running so it can consume commands; STOP stops the
market worker, not the command consumer process.

Set `NANODELTA_COMMAND_POLL_SECONDS` to a positive polling interval (default: one
second). STOP and DRAIN are bounded by `NANODELTA_DRAIN_TIMEOUT_SECONDS`; a timed-out
worker is cancelled and the command is marked failed. A command left in `RUNNING`
after abrupt process death is not automatically
replayed in this checkpoint; operators must diagnose it before issuing a new
idempotent command.

## Historical repair

Set `NANODELTA_HISTORY_ENABLED=true` on the API and configure the same provider
credentials, symbol universe, and `NANODELTA_HISTORY_TIMEFRAMES` used by the market
runtime. The API then constructs market-isolated `BackfillEngine` instances backed by
PostgreSQL watermarks, Silver coverage, and history-run records.

`POST /api/{market}/history-repair` performs only the explicitly supplied gaps for a
configured symbol/timeframe. If history is disabled or the requested tuple is outside
the configured universe, the API returns `501 HISTORY_REPAIR_UNSUPPORTED`; it does not
claim that a repair ran. Provider fallback follows the capability registry.

This wiring does not prove provider credentials, entitlement, quotas, or a sustained
external-provider run. Those require a credentialed acceptance environment.
