# Observability

DocMind AI ships three independent, standard pillars. All three are safe
defaults in development and all three are required reading before go-live in
staging/production.

## Structured logs

`app.logging_config.configure_logging()` runs once at process start (both the
API in `app.main` and the worker in `app.worker`) and switches the root logger
to a JSON formatter. Every record becomes one line of JSON with `timestamp`,
`level`, `logger`, `message`, `service`, plus whatever structured fields were
passed via `extra={...}`:

```python
logger.info("extraction requires review", extra={"document_id": str(document.id), "issue_count": 2})
```

Controlled by:

- `LOG_LEVEL` (default `INFO`)
- `LOG_FORMAT` (`json` default, or `text` for a human-friendly local console)

Ship stdout from both the `api` and `worker` containers to your log
aggregator (CloudWatch Logs, Loki, Datadog, etc.) as-is — no agent-side JSON
parsing config should be required.

## Metrics (Prometheus)

`GET /metrics` on the API exposes Prometheus text-format metrics:

| Metric | Type | Labels | What it tells you |
|---|---|---|---|
| `docmind_http_requests_total` | counter | `method`, `route`, `status` | Request volume and error rate per endpoint |
| `docmind_http_request_duration_seconds` | histogram | `method`, `route` | API latency |
| `docmind_document_processing_duration_seconds` | histogram | — | End-to-end worker processing time |
| `docmind_documents_processed_total` | counter | `status` (`completed`/`failed`) | Pipeline throughput and failure rate |
| `docmind_extraction_requires_review_total` | counter | — | How often the validation layer flags a run |
| `docmind_webhook_deliveries_total` | counter | `success` | Outbound webhook delivery health |

`/metrics` is intentionally unauthenticated, the same as `/health` — restrict
network access to it at the load balancer/ingress in staging/production
rather than behind application auth, so Prometheus itself doesn't need
tenant credentials.

### Restricting `/metrics` at the ingress

Labels never carry tenant data: the `route` label is the route template
(`/api/v1/documents/{document_id}`), and any path that matches no route is
grouped as `unmatched` (`tests/test_metrics_labels.py`). What remains
exposed is the operational picture (volumes, error rates, latency), which
should still stay off the public internet. Two options:

1. **Network allowlist (preferred).** Only the Prometheus host may reach
   the path. Example with nginx in front of the API:

   ```nginx
   location = /metrics {
       allow 10.20.0.0/24;   # Prometheus scrape network
       deny  all;
       proxy_pass http://api:8000;
   }
   ```

2. **Bearer token.** Set `METRICS_TOKEN` in the API environment (a secret,
   not in the repository) and configure Prometheus with
   `authorization: { credentials: <token> }`. Requests without the header get
   `401 METRICS_TOKEN_REQUIRED`. The check is optional: when the variable is
   unset, `/metrics` is open, so option 1 is still needed in that case.

Not verified here: no staging ingress exists in this repository, so neither
example has been tested against a real proxy.

Cardinality guard: do not add labels built from document ids, user ids,
filenames, tenant names or error messages. Review new `Counter`/`Histogram`
definitions in `app/metrics.py` for this.

Bring up a local Prometheus + Grafana to try it:

```bash
docker compose --profile observability up --build
# Prometheus: http://localhost:9090
# Grafana:    http://localhost:3001 (admin / $GRAFANA_ADMIN_PASSWORD, default "admin")
```

Add Prometheus as a Grafana data source (`http://prometheus:9090`) and build
dashboards from the metrics above.

## Traces (OpenTelemetry)

Tracing is off by default (no collector required for local dev or CI) and
turns on the moment `OTEL_EXPORTER_OTLP_ENDPOINT` is set. When set,
`app.telemetry.configure_tracing` / `configure_worker_tracing`:

- instrument FastAPI (one span per request, with route templates),
- instrument SQLAlchemy (one span per query),
- instrument Celery (one span per task, so an API request's trace continues
  into `process_document` / `dispatch_webhooks` running on the worker),
- export everything as OTLP/HTTP batches.

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
OTEL_SERVICE_NAME=docmind-api   # docmind-worker for the worker process
```

The bundled `otel-collector-config.yml` just logs received spans
(`exporters: [debug]`) — point its `exporters` section at Tempo, Jaeger, or a
vendor OTLP endpoint for anything beyond local experimentation, and reference
that exporter in the `traces` pipeline.

```bash
docker compose --profile observability up --build
```

## Alert starting points

A reasonable first pass for staging/production alerting, once the above is
wired into your monitoring stack:

- `rate(docmind_documents_processed_total{status="failed"}[15m])` trending up
- `docmind_http_requests_total{status=~"5.."}` error-rate spikes
- `docmind_webhook_deliveries_total{success="false"}` trending up (a tenant's
  endpoint, or DNS/SSRF validation, is rejecting deliveries)
- p95 of `docmind_document_processing_duration_seconds` approaching
  `PROCESSING_TIMEOUT_SECONDS`

---
Relacionado: [[empresa]]
