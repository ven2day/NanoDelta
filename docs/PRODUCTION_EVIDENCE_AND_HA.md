# Production evidence and optional HA contract

NanoDelta does not become highly available because this directory exists. The committed state is
`NOT_RUN`: no credentialed provider soak, database outage, managed failover, isolated restore or
on-call delivery performed by these files is claimed as production evidence.

## Safety and evidence rules

- Run only on an approved VPS and set `NANODELTA_ACCEPTANCE_EXTERNAL_CONFIRMED=true`.
- Set `NANODELTA_ACCEPTANCE_ENVIRONMENT` and `GITHUB_SHA`; generated JSON belongs in the protected
  release evidence store, not in Git.
- The runner writes `PASSED` only after scenario-specific measurements meet the supplied limits.
  Missing opt-in, tools, metrics, receiver acknowledgement or recovery produces `FAILED` and a
  non-zero exit. The repository template remains `NOT_RUN`.
- Provider credentials stay in the running services. The acceptance runner reads bounded metrics,
  never credential values.
- `timescale-recovery` is only for the repository's local Compose database and requires an explicit
  disruption flag. Managed database failover must use the provider's reviewed control plane.
- `backup-restore` restores only to a newly generated `nanodelta_restore_*` database and drops it in
  `finally`; it never restores over the source database.

Run from the repository root:

```bash
export NANODELTA_ACCEPTANCE_EXTERNAL_CONFIRMED=true
export NANODELTA_ACCEPTANCE_ENVIRONMENT=production-vps-1
export GITHUB_SHA="$(git rev-parse HEAD)"

python scripts/acceptance/run.py provider-soak \
  --duration 21600 --minimum-events 10000 --evidence /secure/evidence/provider-soak.json

python scripts/acceptance/run.py load-latency \
  --requests 5000 --concurrency 50 --maximum-p95 1 \
  --evidence /secure/evidence/load-latency.json

# An operator must interrupt the approved primary provider during this bounded window.
python scripts/acceptance/run.py provider-failover \
  --wait 300 --evidence /secure/evidence/provider-failover.json

# Local Compose database only; causes an intentional outage.
python scripts/acceptance/run.py timescale-recovery \
  --allow-service-disruption --recovery-timeout 180 \
  --evidence /secure/evidence/timescale-recovery.json

python scripts/acceptance/run.py backup-restore \
  --confirm-disposable-restore --destination /secure/backups \
  --evidence /secure/evidence/backup-restore.json

# receipt-url must be an approved receiver audit endpoint that returns the delivered evidence ID.
python scripts/acceptance/run.py alert-delivery \
  --receipt-url https://approved-receiver.example/evidence/latest \
  --evidence /secure/evidence/alert-delivery.json
```

## External managed TimescaleDB

`deploy/ha/docker-compose.external-timescale.yml` is a standalone application manifest with no
local database container. It points API, migrations and the paper runtime at one provider-managed
writer endpoint using a file-mounted password. Use immutable image digests and TLS-enforcing
provider networking. The provider—not Compose—must supply replicas, quorum, fencing, automatic
promotion, backups and point-in-time recovery.

Before deployment, complete `deploy/ha/managed-timescale-requirements.yml` with approved RPO/RTO and
retention values. Deploying the manifest proves only endpoint connectivity. To claim HA, retain:

1. provider topology and multi-zone configuration export;
2. pre-failover server identity and application health;
3. approved provider failover event/audit record;
4. post-failover server identity, data-loss measurement and application recovery time;
5. an independently executed backup/restore evidence report;
6. Alertmanager receiver acknowledgement.

Never imitate HA with two writable PostgreSQL containers or DNS switching without fencing. A
primary/replica design is acceptable only when the database platform owns replication, promotion,
split-brain prevention and durable client routing.

## Evidence interpretation

| Scenario | What `PASSED` establishes | What it does not establish |
|---|---|---|
| Provider soak | event flow and bounded cycle errors for the measured window | provider SLA or future availability |
| Load/latency | measured endpoint p50/p95 and error rate | trading decision latency unless that endpoint is targeted |
| Provider failover | failover counter increased and events continued | broker correctness or lossless sequencing |
| Timescale recovery | local DB outage observed and API readiness recovered | managed HA |
| Backup/restore | checksum, isolated logical restore, schema verification, measured RPO/RTO | PITR or regional DR |
| Alert delivery | approved receiver returned the unique evidence ID | full on-call response performance |

The committed `scripts/acceptance/evidence-not-run.json` is the authoritative repository status
until protected, environment-specific reports are produced and reviewed.
