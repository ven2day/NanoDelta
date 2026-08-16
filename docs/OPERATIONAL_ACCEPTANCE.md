# Operational acceptance

NanoDelta uses evidence-producing acceptance checks rather than a generic
"production-grade" label. The report format is defined by
`docs/acceptance-report.schema.json`.

## Profiles

The CI-safe quick profile exercises the real FastAPI ASGI application, append-only
decision runtime, all three supervised market workers, deterministic provider and
database fault recovery, scheduler drift, and graceful drain:

```bash
python scripts/run_acceptance.py --profile quick \
  --output artifacts/acceptance-report.json
```

The full profile is deliberately opt-in because it runs a one-hour three-market
worker soak and 10,000 API and decision operations:

```bash
NANODELTA_RUN_FULL_ACCEPTANCE=1 \
NANODELTA_ACCEPTANCE_ENV=staging-vps \
python scripts/run_acceptance.py --profile full \
  --output artifacts/full-acceptance-report.json
```

| Check | Pass threshold |
|---|---:|
| In-process API p95 | <= 100 ms |
| In-process decision append p95 | <= 100 ms |
| Success ratio | 100% |
| Scheduler maximum drift | <= 40 ms |
| Graceful drain | <= 500 ms |
| Provider/database recovery | <= 3 attempts |

These are local component SLOs. They do not establish external provider latency,
VPS capacity, network availability, or market-session reliability. Those results
must be generated in the named target environment and retained as artifacts.

## Backup and restore drill

```bash
scripts/backup.sh backups
scripts/verify-backup-restore.sh backups/nanodelta-YYYYMMDDTHHMMSSZ.dump
```

The verifier checks the SHA-256 sidecar, starts a disposable TimescaleDB container
without publishing a host port or mounting the production volume, restores the
dump, queries `control.schema_migrations`, and deletes the container. A passing
drill proves only the dump under test was restorable with the pinned image.

## Failure, recovery, and evidence scope

- Provider recovery uses deterministic transient connection failures.
- Database reconnect is simulated at the connection-operation boundary.
- Worker soak covers NSE, Forex, and Crypto equally and checks bounded drain.
- Real database outage, provider disconnect, machine restart, and network partition
  tests remain staging requirements.
- Full profiles never run implicitly in unit tests or application startup.
- Commit schemas and harnesses, not fabricated results. Generated reports belong in
  CI/staging artifacts and must name the environment.
- Any `FAIL` result fails the command. `SKIP` is non-failing but remains visible.
