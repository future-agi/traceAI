# traceai-cognee

Send Cognee's native OpenTelemetry spans to Future AGI without additional instrumentation. Enable `COGNEE_TRACING_ENABLED=true` before your first Cognee operation and configure once before that operation.

Requires Python 3.10–3.13 and `fi-instrumentation-otel>=1.2.2,<2`.

```bash
pip install "cognee[tracing]==1.6.5" "traceai-cognee==0.1.0"
export COGNEE_TRACING_ENABLED=true
export FI_API_KEY="YOUR_FI_API_KEY"
export FI_SECRET_KEY="YOUR_FI_SECRET_KEY"
export FI_PROJECT_NAME="my-cognee-app"
```

Configure your model and embedding providers using the [Cognee documentation](https://docs.cognee.ai/), then run your operations with the adapter attached:

```python
import os
from traceai_cognee import configure

handle = configure(project_name=os.environ['FI_PROJECT_NAME'])
# Run cognee.add, cognee.cognify and cognee.search here.
handle.force_flush()
handle.shutdown()
```

Set `FI_API_KEY` and `FI_SECRET_KEY` for the Future AGI exporter. `FI_BASE_URL` selects a different Future AGI origin. Alternatively supply an OTLP HTTP `endpoint` and `headers`, or an injected OpenTelemetry exporter. A supplied exporter is owned by the handle. `provider` can be the global SDK provider Cognee will use. Existing processors are not shut down; a provider whose default processor would be replaced on add is rejected.

A repeated `configure` with the same options returns the active handle; different options require shutting it down first. Shutdown detaches the handle immediately and closes the exporter asynchronously, after in-flight callbacks return; it does not wait for exporter closure. Flush before shutdown when the queue must be drained. Each span is exported by the configuration that was active when it started: end spans and flush before shutdown, because a span still open at shutdown is not exported, and a later configuration does not adopt it. The adapter tracks at most 65,536 open spans; beyond that the oldest open span is dropped when it ends. Spans that share a trace ID, span ID and start time with another tracked span are not exported; such reuse only happens with a faulty ID generator, and once the limit evicts the earlier span's entry the protection no longer applies to it. If a custom provider raises from `add_span_processor` after attaching the processor, a retry reuses it; a provider that hides its processor list can keep an inactive extra processor for each failed attempt. Closure uses a non-daemon thread, so a stuck exporter can delay interpreter exit. An old handle cannot flush or shut down a later configuration. Shutting down the provider closes the adapter too and prevents reuse of that provider. Overlapping `configure` calls fail fast with `RuntimeError`; retry after the first call finishes. A successful `force_flush` drains the processor queue, not an acknowledgement of remote delivery.

By default only allowlisted operational attributes are sent. `capture_content=True` includes recognized Cognee input/output fields. Error details and stack traces remain excluded. Span names are retained; keep sensitive data out of them. Other processors on the provider receive unmodified native spans and need their own privacy controls.

### Structured content redaction

With content capture enabled, the adapter redacts recognized credential field names in nested objects, arrays, JSON-serialized objects, and flattened paths. These include API keys, authorization headers, access/refresh/auth/bearer/ID/session/security tokens, client secrets, secret/private keys, passwords, credentials, and AWS access-key IDs and secret access keys. Common snake-case, camel-case, and dotted/bracketed path forms are recognized. Fields such as `input_tokens`, `token_count`, and `request_id` remain available.

This is a field-name policy, not a universal secret detector. Free text and unknown encodings are not scanned for credentials; enable content capture only for approved application data. The adapter does not mutate the original content or inspect opaque runtime objects. Numeric attributes outside the OTLP signed 64-bit integer range, or non-finite floating-point values, are omitted before encoding.

For a complete add/cognify/search application, see the [Cognee example](../../examples/cognee/). Search spans are mapped to `RETRIEVER` and structured-output model calls to `LLM`. Native span IDs, parent relationships and status codes are preserved, including Cognee's missing-collection probe errors.
