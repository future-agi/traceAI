# traceAI for .NET

`fi-instrumentation-otel` provides OpenTelemetry tracing for .NET AI applications.
Create typed LLM, agent, tool, and retrieval spans, propagate trace context, and
export telemetry to Future AGI over OTLP.

[Documentation](https://docs.futureagi.com/docs/tracing) · [Source](https://github.com/future-agi/traceAI/tree/dev/csharp/src/FIInstrumentation) · [Issues](https://github.com/future-agi/traceAI/issues)

## Install

This package targets .NET 10.

```sh
dotnet add package fi-instrumentation-otel
```

Set `FI_API_KEY`, `FI_SECRET_KEY`, and `FI_PROJECT_NAME` in your environment before
starting the application. Keep credentials outside source control.

## Quick start

```csharp
using FIInstrumentation;

var tracer = TraceAI.RegisterFromEnvironment();
try
{
    tracer.Trace("example", FISpanKind.Chain, span =>
    {
        // Put your application work here.
    });
}
finally
{
    TraceAI.Shutdown();
}
```

This creates a manual span, not an LLM request. Provider SDKs and their credentials
remain the application's responsibility. Use `TraceAI.Register` to configure
tracing in code, and `TraceConfig` to control sensitive attribute capture.
`TraceAI.Shutdown()` flushes pending telemetry when the application exits.

## Suppress tracing for sensitive work

Wrap work that must not create FI spans in `SuppressTracing`. Your operations still
run and return results or throw their original exceptions. Newly created FI spans
do not capture or serialize values.

```csharp
using FIInstrumentation.Context;

using (new SuppressTracing())
{
    tracer.Chain("private-operation", span =>
    {
        // Put sensitive application work here.
    });
}
```

Suppression flows across `await` and into child tasks. Nested scopes keep the outer
scope suppressed until it ends. It also uses OpenTelemetry's native suppression
scope, and FI spans respect an existing `SuppressInstrumentationScope`.

This prevents **new** spans; it does not erase existing spans or stop you from
adding data to an already active span. Other telemetry libraries must honor
OpenTelemetry suppression to be covered. A suppressed `FISpan.Activity` is an
unstarted placeholder: do not call `Start()` on it or use it as a parent.

## License

Apache-2.0. The package icon is the Future AGI GitHub organization's logo.
