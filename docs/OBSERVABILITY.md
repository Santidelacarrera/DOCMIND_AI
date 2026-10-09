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
