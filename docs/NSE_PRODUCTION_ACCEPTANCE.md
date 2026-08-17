# NSE production paper-trading acceptance

Repository status: **NOT RUN**. The committed template at
`scripts/acceptance/nse/evidence-not-run.json` is authoritative until an operator runs the suite in
the target environment and reviewers retain the generated report outside Git.

This runbook validates one deployed NSE **paper-trading** release. It does not enable or certify
live broker execution, profitability, exchange membership, regulatory compliance, managed
TimescaleDB high availability, regional disaster recovery or a future provider SLA.

## What must pass

The single suite command runs these gates in order and stops at the first failure:

1. **Dhan history and readiness** — every enabled NSE symbol must have settled Dhan candles for
   5m, 15m, 30m and 1h reaching at least 730 days back, a recent candle, and a latest successful
   Dhan history run. Minimum settled-candle counts (35,000/11,500/6,000/3,200 respectively)
   guard against endpoint-only coverage with large internal gaps. The authoritative universe API
   total must equal the database total.
2. **TrueData realtime soak** — TrueData must remain the healthy active NSE provider for the
   measured window. NSE-labelled event, cycle-error, sequence-gap and failover counter deltas are
   recorded. The production default is six hours and at least 10,000 events with no cycle errors,
   gaps or failovers.
3. **TimescaleDB paper lifecycle** — a recent durable chain must join a BUY/SELL candidate to an
   approved paper decision, filled paper order, position, closed exit plan and outcome. Gold
   snapshot IDs and the signal/scoring/portfolio/risk/execution stages must exist, and the records
   must be visible through authoritative APIs.
4. **Runtime restart and idempotency** — the runner restarts only the Compose `runtime` service,
   waits for a newer durable heartbeat and provider events, verifies durable counters and sequence
   state never regress, and verifies there are no duplicate order idempotency keys, fills or
   outcomes.
5. **Provider failover and recovery** — during an approved TrueData interruption, the durable feed
   state must switch to Dhan, its failover counter must increase and Dhan events must continue.
   After TrueData is restored it must become healthy and produce new events again.
6. **Backup and restore** — delegates to the existing production acceptance runner. It creates a
   logical backup, checksums it, restores it into a newly generated disposable database, verifies
   migrations, measures RPO/RTO and drops the disposable target. It never restores over the source.
7. **Decision latency** — measures deltas from the NSE-labelled
   `nanodelta_decision_pipeline_duration_seconds` histogram. The default requires at least 100
   observations, p95 at or below one second and no error observations.
8. **Alertmanager receipt** — delegates to the existing alert-delivery runner and passes only when
   the configured receiver returns the unique evidence ID.

Database connections are put into read-only mode. The only database writes are made by the already
running application and the existing isolated backup/restore runner. The suite never submits a
broker or paper order to manufacture evidence.

## Preconditions

- Check out the exact immutable release being accepted; the supplied SHA must equal `HEAD`.
- Start the production API, NSE runtime, TimescaleDB and observability stack with real Dhan and
  TrueData credentials mounted as files.
- Run during an approved NSE paper session with the configured annual NSE holiday calendar.
- Complete the Dhan two-year backfill and allow at least one real paper position to exit so a
  durable outcome exists.
- Configure an Alertmanager receiver with an auditable receipt endpoint.
- Store the database URL, API key, operator confirmation and generated evidence outside Git with
  restrictive permissions. Provider identity arguments are non-secret aliases, not usernames,
  client IDs, access tokens or passwords.
- Obtain a change window for the runtime restart, TrueData interruption and disposable restore.

The operator confirmation is a protected JSON file. Every scenario requires a separate reference;
one global boolean is not sufficient:

```json
{
  "schema_version": "1.0",
  "environment": "nse-paper-prod-1",
  "release_sha": "FULL_GIT_SHA",
  "approved_by": "operator-name",
  "approved_at": "2026-08-17T03:00:00+00:00",
  "change_ticket": "CHG-12345",
  "scenarios": {
    "dhan_history_readiness": {"confirmed": true, "reference": "CHG-12345/history"},
    "truedata_realtime_soak": {"confirmed": true, "reference": "CHG-12345/soak"},
    "timescaledb_paper_lifecycle": {"confirmed": true, "reference": "CHG-12345/lifecycle"},
    "runtime_restart_recovery": {"confirmed": true, "reference": "CHG-12345/restart"},
    "provider_failover": {"confirmed": true, "reference": "CHG-12345/failover"},
    "backup_restore": {"confirmed": true, "reference": "CHG-12345/restore"},
    "decision_latency": {"confirmed": true, "reference": "CHG-12345/latency"},
    "alertmanager_receipt": {"confirmed": true, "reference": "CHG-12345/alert"}
  }
}
```

## Single orchestrator command

Run from the release checkout. This is the only suite command required; it invokes the existing
backup/restore and alert-delivery acceptance implementations rather than copying them.

```bash
release_sha="$(git rev-parse HEAD)"

python scripts/acceptance/nse/run.py suite \
  --evidence /secure/nanodelta/evidence/nse-${release_sha}.json \
  --confirmation /secure/nanodelta/approvals/nse-${release_sha}.json \
  --environment nse-paper-prod-1 \
  --release-sha "${release_sha}" \
  --dhan-provider-identity dhan-production-primary \
  --truedata-provider-identity truedata-production-realtime \
  --database-url-file /run/secrets/nanodelta_acceptance_database_url \
  --api-key-file /run/secrets/nanodelta_acceptance_api_key \
  --api-url http://127.0.0.1:8000 \
  --metrics-url http://127.0.0.1:9101/metrics \
  --compose-file docker-compose.yml \
  --allow-runtime-restart \
  --allow-provider-interruption \
  --confirm-disposable-restore \
  --backup-destination /secure/nanodelta/backups \
  --alertmanager-url http://127.0.0.1:9093 \
  --receipt-url https://approved-receiver.example/evidence/latest
```

The operator must perform the approved TrueData interruption when prompted, then restore TrueData
when prompted. Do not automate a provider outage with credential deletion or an unreviewed network
rule.

For a shorter rehearsal, thresholds and durations can be overridden, but the result is not
production acceptance unless the reviewed release policy explicitly approves those values. The
generated report records the actual thresholds and measurements.

## Fail-closed behavior and evidence handling

- Missing confirmation, a mismatched release/environment, missing metrics, unavailable API or
  database, absent lifecycle records, an unmet threshold or an unacknowledged alert produces
  `FAILED` and a non-zero exit.
- Scenarios after the first failure remain `NOT_RUN`; a partial run is never promoted to `PASSED`.
- The suite-level status becomes `PASSED` only when all eight scenario statuses are `PASSED`.
- The report binds the checked-out release SHA, environment ID, non-secret provider aliases,
  confirmation-file checksum, approver, change ticket, scenario references, timestamps,
  thresholds and measurements.
- Generated evidence and backups remain outside Git. Upload them to the protected release evidence
  store, retain Alertmanager receiver audit data and have a second operator review the package.
- Never change the committed `NOT_RUN` template to `PASSED`. It is a status template, not an
  environment report.

## Promotion decision

All eight gates plus deployment, security and operational review can support the statement:

> This exact release passed the reviewed production acceptance policy for NSE paper trading in the
> named single-host environment during the recorded window.

They do **not** support these statements:

- the UI or system will always be available;
- strategies are profitable or suitable for live capital;
- Dhan order execution is implemented or approved;
- the database is highly available;
- backups provide point-in-time, off-host or regional disaster recovery;
- the environment is production-ready for Forex or Crypto.

Live trading requires a separately designed broker execution authority, reconciliation, regulatory
controls and acceptance program. HA requires a managed or correctly fenced replicated database,
automated failover and separately measured RPO/RTO; this single-host suite deliberately does not
claim either.
