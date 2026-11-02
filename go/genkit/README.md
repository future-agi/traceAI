# Genkit Go export privacy

Genkit Go 1.14.0 emits native OpenTelemetry GenAI spans. This module wraps a standard OpenTelemetry trace exporter to apply Future AGI privacy switches to those spans before they leave the process. It does not instrument models, add spans, or configure an endpoint. Requires Go 1.25 or later.

```sh
go get github.com/firebase/genkit/go@v1.14.0 github.com/future-agi/traceAI/go/genkit@dev
```

After a release tag exists, use `@latest` rather than `@dev`. Set the switches before constructing the tracer provider:

```sh
export FI_HIDE_INPUTS=true
export FI_HIDE_OUTPUTS=true
export FI_HIDE_ERROR_DETAILS=true
```

Wrap your existing trace exporter **once**:

```go
import (
    traceaigenkit "github.com/future-agi/traceAI/go/genkit"
    "github.com/firebase/genkit/go/core/tracing"
    "github.com/firebase/genkit/go/plugins/otelgenai"
    sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// exporter is your configured OTLP trace exporter.
tp := sdktrace.NewTracerProvider(sdktrace.WithBatcher(
    traceaigenkit.NewExporter(exporter, traceaigenkit.ConfigFromEnv()),
))
tracing.SetInstrumentation(otelgenai.New(&otelgenai.Options{
    TracerProvider: tp,
    ContentCapturingMode: otelgenai.NoContent,
    CaptureActionIO: false,
    DisableMetrics: true,
}))
// Initialize Genkit and run your application; shut down tp on exit.
```

`FI_HIDE_INPUTS` drops model input messages, system instructions, and raw action input. `FI_HIDE_OUTPUTS` drops model output messages and raw action output. Both apply even if native content capture is changed to `SpanOnly`. `FI_HIDE_ERROR_DETAILS` removes status descriptions and exception details on every span sent through this provider, including flow/parent spans. Error status codes remain. Application return values and errors are unchanged.

Choose one provider and pass only the wrapped exporter to it; using the raw exporter in parallel bypasses redaction and duplicates spans. Other exporters or Genkit telemetry plugins are separate paths and are not protected by this adapter. Custom attributes, span names, resource attributes, and explicit telemetry logs can carry sensitive data; avoid recording secrets in them. The adapter snapshots these switches at construction, so restart after changing them.
