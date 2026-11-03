# fi-instrumentation-otel

Create application spans and export them to Future AGI with a shared OpenTelemetry provider. Framework-specific instrumentation is available through the [Python integration packages](../README.md#packages).

## Quick start

### Install

```bash
pip install fi-instrumentation-otel
```

### Configure authentication

Set your Future AGI credentials in the environment before starting your application:

```bash
export FI_API_KEY="your-future-agi-api-key"
export FI_SECRET_KEY="your-future-agi-secret-key"
```

### Create a span

Save as `trace_example.py`, then run `python trace_example.py`:

```python
from fi_instrumentation import FITracer, register
from fi_instrumentation.fi_types import ProjectType

tracer_provider = register(
    project_name="my-ai-project",
    project_type=ProjectType.OBSERVE,
)
tracer = FITracer(tracer_provider.get_tracer(__name__))

try:
    with tracer.start_as_current_span("my-llm-span") as span:
        # Replace this with your application's work and attributes.
        span.set_attribute("my-attribute", "my-value")
finally:
    tracer_provider.shutdown()  # Export pending spans before the process exits.
```

Open the `my-ai-project` project in **Observe** in [Future AGI](https://app.futureagi.com) to inspect `my-llm-span` and its `my-attribute` attribute.

In a long-running application, create one provider during startup and shut it down from the application's shutdown hook, not after each request. See the [Python shutdown guide](../README.md#shutdown-on-termination) for termination signals.

Refer to the [OpenTelemetry Python documentation](https://opentelemetry.io/docs/languages/python/) for span APIs and the [FI semantic conventions](https://docs.futureagi.com/docs/observe/features/manual-tracing/semantic-conventions) for application attributes.

## Share the provider with a framework

`register()` returns a provider without installing it as OpenTelemetry's global provider by default. Pass `tracer_provider` through your framework's supported tracer-provider setting so its native spans use the same exporter and project resource.

If a framework only reads OpenTelemetry's global provider, set `set_global_tracer_provider=True` in your initial `register()` call, before initializing the framework. Do not replace a global provider that your application has already configured; follow the framework's integration guide to reuse its existing telemetry pipeline. Avoid enabling a second instrumentor for an operation that already emits native spans.

## Semantic conventions

The `semantic_convention` option to `register()` accepts a `SemanticConvention` from `fi_instrumentation.otel` and records its value on the provider's resource. It is a selector describing the emitted convention, **not** a client-side attribute conversion step: it does not rename span attributes, change their values, or add missing attributes. Use your framework's integration guide for its supported attribute mapping.

## Contributing

This package is part of [traceAI](https://github.com/future-agi/traceAI). See the [contributing guide](../../CONTRIBUTING.md) and [documentation](https://docs.futureagi.com).
