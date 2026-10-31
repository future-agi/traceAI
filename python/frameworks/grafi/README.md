# traceAI-grafi

Send Graphite's native agent, workflow, node and tool spans to Future AGI without copying an exporter into your application. Requires Python 3.11–3.13 and `grafi` 0.0.36.

```bash
pip install traceAI-grafi
export FI_API_KEY="your-api-key"
export FI_SECRET_KEY="your-secret-key"
export FI_PROJECT_NAME="graphite-app"
```

In your existing Graphite application, replace the runtime construction with:

```python
from traceai_grafi import configure

# Inside your application's async entry point; assistant and request are your
# existing Graphite assistant and PublishToTopicEvent.
with configure() as tracing:
    events = [event async for event in tracing.runtime.invoke(assistant, request)]
```

Keep the handle for the application's lifetime. The context manager shuts down its owned provider and exports the final batch. `tracing.force_flush(timeout_millis=5000)` flushes without closing it. You can also call `tracing.shutdown()` explicitly.

Do not call Graphite's `setup_tracing()` or add another provider instrumentor for the same model calls. This adapter binds one tracer through native `ExecutionServices`; it never patches Graphite, sets a global provider or creates additional spans. Native spans do not supply model token usage; the adapter does not invent usage or costs.

## Configuration

- `project_name` or `FI_PROJECT_NAME`: Observe project, default `graphite-app`.
- `base_url` or `FI_BASE_URL`: Future AGI origin, default `https://api.futureagi.com`. The adapter appends `/tracer/v1/traces`. Non-loopback origins must use HTTPS.
- `api_key` / `secret_key` or `FI_API_KEY` / `FI_SECRET_KEY`: both authentication headers are required.
- `capture_content=True` or `FI_CAPTURE_CONTENT=1`: opt in to serialized input/output. Off by default.
- `capture_errors=True`: separately opt in to exception messages and error status descriptions. Off by default; `FI_HIDE_ERROR_DETAILS=true` overrides this option. Content capture does not enable error text.
- `services=ExecutionServices(...)`: retain your event store and error reporter. The supplied tracer must be the default `NoOpTracer`; an already-traced service bundle is rejected rather than overwritten.

For manual runtime composition, `create_tracer_provider()` accepts the same export/privacy options and returns an owned OTel provider. Inject `provider.get_tracer("grafi")` into `ExecutionServices` and shut it down yourself. `GrafiExporter(inner, capture_content=False, capture_errors=False)` supports an application-owned exporter pipeline.

## Captured data

The adapter maps `oi_span_type` to `fi.span.kind`, `conversation_id` to `session.id`, and nonempty `user_id` to `user.id`. Native IDs, timing, parents, kinds and status codes are retained. Input/output are mapped only with content capture enabled.

An allowlist excludes unknown attributes, free-form dictionaries, event payloads, link/scope attributes, tracestate, host/process resources, and Graphite's `error`, `error.stack` and `error.details` fields. Exception stack-trace fields are excluded in both modes. Content/error fields over 65,536 characters are omitted, not truncated. This is export filtering, not a free-text secret detector: opted-in inputs/results/messages can contain sensitive data.

Use static component names and opaque session/user IDs: names and approved metadata are exported. Graphite still records data in its own event store and application logs; this adapter does not redact those or other exporters you configure. Graphite's `GRAFI_SPAN_DISABLE_PAYLOADS=1` and `GRAFI_SPAN_MAX_PAYLOAD_CHARS` remain effective before export.
