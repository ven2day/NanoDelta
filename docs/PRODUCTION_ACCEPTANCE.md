# Production acceptance and evidence

Implementation and production evidence are separate. CI may run the deterministic synthetic
load/latency workload, but it must not label that result as provider, database, failover, backup,
or production evidence.

## Evidence contract

Every report uses `docs/acceptance/evidence-template.json` schema version 1.0 and records scenario,
execution mode, timestamps, commit SHA, status, reason and measurements. `NOT_RUN` and `SKIPPED`
are valid and intentionally different from `PASSED`. Generated evidence belongs in release
artifacts or the approved operations evidence store; credentials and production URLs must not be
committed.

```bash
python scripts/run-acceptance.py load-latency --output /tmp/load-latency.json

# Observe a deployed endpoint. Missing opt-in fails closed when --require-external is set.
NANODELTA_ACCEPTANCE_PROBE_URL=https://approved-host/metrics \
NANODELTA_ACCEPTANCE_EXTERNAL_CONFIRMED=true \
python scripts/run-acceptance.py provider-soak \
  --samples 3600 --interval 10 --require-external --output /secure/evidence/soak.json
```

The external probe proves availability and sampled latency only. For provider failover, database
recovery and backup restore, attach operator action logs and before/after metric snapshots to the
report. The generic probe deliberately marks `operator_action_required=true`; it does not infer
that a disruptive action occurred.

## Required production gates

| Gate | Automated contract | Required external evidence | Current repository evidence |
|---|---|---|---|
| API load/latency | deterministic runner | production-sized run and SLO | NOT RUN |
| Realtime soak | fail-closed endpoint probe | credentialed provider session, counter deltas | NOT RUN |
| Provider failover | metric probe | approved outage injection and recovery log | NOT RUN |
| TimescaleDB recovery | metric probe | isolated failure/recovery drill | NOT RUN |
| Backup restore | checksum plus isolated restore | restored schema/data verification and RTO/RPO | NOT RUN |
| Alert delivery | Prometheus rules and receiver contract | test notification acknowledged by on-call | NOT RUN |
| HA/DR | design matrix below | second environment and failover evidence | NOT IMPLEMENTED |

## HA/DR design matrix

| Layer | Current topology | Production target | Evidence required |
|---|---|---|---|
| API/web | single Compose host | at least two stateless instances behind health-aware routing | instance-loss drill |
| Runtime | one worker process | active/passive lease per market; never two paper writers | lease/fencing drill |
| TimescaleDB | single persistent volume | managed HA or streaming replica with automated failover | failover, RPO and RTO |
| Backups | local scheduled dumps | encrypted off-host immutable copies with retention | monthly isolated restore |
| Metrics/alerts | local Prometheus/Alertmanager | durable remote metrics and approved on-call receiver | notification and retention proof |

Until those external rows pass, describe NanoDelta as pre-production paper-trading infrastructure.
