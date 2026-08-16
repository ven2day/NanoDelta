# Production observability

NanoDelta exposes structured request logs and bounded-cardinality Prometheus metrics. The optional Compose observability profile runs Prometheus, Alertmanager, and a provisioned Grafana dashboard.

## Start the stack

Create the Grafana administrator password beside the existing deployment secrets:

```bash
openssl rand -base64 48 > secrets/grafana_admin_password
chmod 600 secrets/grafana_admin_password
docker compose --profile observability up -d
```

All observability ports bind to loopback by default:

- API metrics: `http://127.0.0.1:8000/metrics`
- Prometheus: `http://127.0.0.1:9090`
- Alertmanager: `http://127.0.0.1:9093`
- Grafana: `http://127.0.0.1:3001`

Use an authenticated TLS reverse proxy or an SSH tunnel for remote access. Do not expose these ports directly to the internet.

## Logs and correlation

Production API logs are one JSON object per line. A valid inbound `X-Correlation-ID` is preserved; otherwise the API generates a UUID. The response always returns the effective ID. Request logs include the method, templated route, status, market scope, and duration. Query strings, request bodies, credentials, API keys, symbols, and candidate IDs are not logged by the HTTP middleware.

The supported market label values are `nse`, `forex`, `crypto`, and `global`. Metrics use FastAPI route templates such as `/api/{market}/health`; raw paths, symbols, and candidate IDs are deliberately excluded to prevent cardinality growth.

## Metrics and alerts

The API exports:

- `nanodelta_http_requests_total`
- `nanodelta_http_request_duration_seconds`
- `nanodelta_http_requests_in_progress`
- `nanodelta_market_worker_state`
- `nanodelta_market_heartbeat_age_seconds` (`-1` means no heartbeat has been observed)

Prometheus provisions alerts for API and runtime scrape failure, error rates, sequence gaps and
sustained latency. Alertmanager uses a local no-credential receiver by default. To configure a real
receiver, copy `deploy/observability/alertmanager/receiver.example.yml` outside the checkout,
replace its URL through approved secret management, restrict it to mode `0600`, and set
`ALERTMANAGER_CONFIG_PATH` to that host-managed file. The committed example is not a receiver.

The Grafana dashboard shows request rate by market, server-error ratio, p95 latency, and outcomes by templated route.

## Operational verification

```bash
curl -fsS http://127.0.0.1:8000/metrics | grep nanodelta_http
curl -fsS http://127.0.0.1:9090/-/healthy
curl -fsS http://127.0.0.1:9093/-/healthy
docker compose --profile observability logs --since=10m api prometheus alertmanager grafana
```

The runtime endpoint on port 9101 exports bounded provider-event, WebSocket failover/sequence-gap,
runtime-cycle, database-operation and paper-decision latency metrics. Labels never contain symbols,
accounts, candidates, orders or exception text. This checkpoint still does not provide centralized
logs, on-call credentials, a database exporter, tracing, or a demonstrated production monitoring
run. Evidence requirements are in [PRODUCTION_ACCEPTANCE.md](PRODUCTION_ACCEPTANCE.md).
