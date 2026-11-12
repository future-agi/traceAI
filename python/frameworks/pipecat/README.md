# Pipecat OpenTelemetry integration

Send Pipecat's native conversation, turn and service spans to Future AGI. Pipecat creates the spans; this package maps their input/output, tool, token and conversation attributes for **Observe** without adding a second span producer.

## Install

Use an existing Pipecat bot with its services and transport configured. Keep the extras your bot needs in addition to `tracing`.

```bash
pip install --upgrade traceAI-pipecat "fi-instrumentation-otel>=1.2.2" "pipecat-ai[tracing]"
```

## Configure authentication

```bash
export FI_API_KEY="your-futureagi-api-key"
export FI_SECRET_KEY="your-futureagi-secret-key"
```

For self-hosted Future AGI, also set `FI_BASE_URL` to your collector origin.

**Content privacy:** Native Pipecat spans can contain transcripts, prompts, model output, synthesized text, tool arguments/results and error details. Mapping preserves native attributes; it is not a content filter. Do not rely on `FI_HIDE_INPUTS` or `FI_HIDE_OUTPUTS` to remove native Pipecat content. Leave native tracing disabled if exporting that content is incompatible with your data policy.

## Configure one exporter

Run this once at startup, before constructing your pipeline. Pass the mapped exporter directly to `register()` instead of inspecting or replacing a running provider's processors.

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.otel import HTTPSpanExporter
from traceai_pipecat import MappedSpanExporter

exporter = HTTPSpanExporter(timeout=10.0)
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="Pipecat Voice App",
    span_exporter=MappedSpanExporter(exporter),
    set_global_tracer_provider=True,
    verbose=False,
)
```

`MappedSpanExporter` retains the configured exporter itself, including explicit headers, timeout, compression, TLS settings and custom session. It forwards export results, flushes and shutdown. Attach only the wrapper, not both exporters. When using the provider above, do not separately call Pipecat's `setup_tracing()` or install another exporter for the same destination.

Enable Pipecat's native tracing and turn tracking in your existing pipeline worker, then run your bot normally. See the [Pipecat tracing guide](https://docs.pipecat.ai/server/utilities/opentelemetry) for the API matching your Pipecat version.

After the runner stops and its conversation spans end, flush and shut down once:

```python
trace_provider.force_flush()
trace_provider.shutdown()
```

Open **Observe** and select **Pipecat Voice App**. Available service, model and usage fields depend on what your bot emits.

### gRPC

Install `fi-instrumentation-otel[grpc]>=1.2.2`. Use `MappedSpanExporter(GRPCSpanExporter(...))` with the same setup, importing `GRPCSpanExporter` from `fi_instrumentation.otel`. Set `FI_GRPC_URL` for a self-hosted gRPC collector, or pass an explicit endpoint and TLS configuration to the exporter.

### Existing applications

The `enable_http_attribute_mapping()`, `enable_grpc_attribute_mapping()` and `enable_fi_attribute_mapping(Transport.HTTP)` helpers remain supported. Call the appropriate helper after registering your global provider and **before** producing spans. They wrap existing FI exporters without reconstructing their options. Repeated calls do not stack wrappers; `True` also means a matching exporter was already mapped. `False` means no matching exporter could be mapped.

These compatibility helpers inspect OpenTelemetry processor internals. Prefer explicit `MappedSpanExporter` composition for new applications.

## API reference

- `MappedSpanExporter(exporter)`: wrap any configured OpenTelemetry `SpanExporter`; the wrapper owns its lifecycle through the processor. Do not shut down the delegate separately.
- `create_mapped_http_exporter(endpoint=None, headers=None, **kwargs)`: create a mapped FI HTTP exporter; forward HTTP options such as `timeout`, `compression`, `session` and TLS settings unchanged.
- `create_mapped_grpc_exporter(endpoint=None, headers=None, **kwargs)`: create a mapped FI gRPC exporter; forward options such as `timeout`, `insecure`, `credentials` and `compression` unchanged.
- `MappedHTTPSpanExporter`, `MappedGRPCSpanExporter` and `BaseMappedSpanExporter`: existing exporter classes remain available.

## Troubleshooting

- **No spans:** enable native tracing in your pipeline and use one global provider. Mapping does not create spans itself.
- **No matching exporter:** for compatibility helpers, call `register()` first with `set_global_tracer_provider=True` and choose the matching HTTP or gRPC helper.
- **Last spans are missing:** let the runner finish, then flush and shut down the provider. Do not shut it down after each conversation in a shared process.
- **Sensitive content appears:** disable native tracing if this data cannot leave your application. Attribute mapping alone does not redact it.
