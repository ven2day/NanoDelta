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

## Safety and current boundary

The executable currently proves scheduling, state persistence, failure isolation
and graceful lifecycle for all three markets. Its composition callback is
intentionally idle until each provider's realtime stream and the authoritative
paper decision pipeline are wired and integration-tested. It cannot place live
broker orders. Qwen/LLM services remain advisory and have no runtime authority.

This checkpoint is not evidence of a completed realtime provider session or a
demonstrated end-to-end paper-trading session.
