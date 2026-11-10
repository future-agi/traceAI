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

## Hide sensitive content

Configure masking before registering your tracer:

```csharp
var config = TraceConfig.Builder()
    .HideInputs()
    .HideOutputs()
    .Build();

var tracer = TraceAI.Register(options => options.TraceConfig = config);
```

These flags also default from `FI_HIDE_INPUTS` and `FI_HIDE_OUTPUTS`. Hidden values
are replaced with `__REDACTED__` before object serialization. Masking applies to
`FISpan` convenience methods, every `SetAttribute` overload, and context attributes
applied by the tracer. Input hiding covers semantic input content such as tool
arguments, system instructions, retrieval queries and prompt-template variables;
output hiding covers tool results and retrieved documents. Flattened descendants
of these fields are covered too. Model names, token counts, tool IDs and
prompt-template names remain available. Content fields that can contain either
direction, such as audio transcripts and aggregate tool calls, are hidden when
either direction is private.

Use `FI_HIDE_INPUT_MESSAGES` or `FI_HIDE_OUTPUT_MESSAGES` to hide message attributes
and their flattened descendants. `FI_HIDE_INPUT_TEXT` and `FI_HIDE_OUTPUT_TEXT`
hide message text; `FI_HIDE_INPUT_IMAGES` hides input images and screenshots.
When a message or content block is stored as one JSON value, text or image hiding
redacts that whole value, including its metadata. For flattened attributes, these
narrower flags preserve sibling metadata and other content types.
`FI_HIDE_EMBEDDING_VECTORS` hides embedding vectors, including indexed values.

Error messages and stack traces can contain the same sensitive content. By
default, enabling any of these input/output hiding flags also omits error details:
status descriptions, `error.message`, `exception.message`, and
`exception.stacktrace`. Error status and exception types remain available.
Set `FI_HIDE_ERROR_DETAILS=true` or call `.HideErrorDetails()` to hide error details
without hiding inputs or outputs. An explicit `false` keeps error details even
when content hiding is enabled; use that override only if those details are safe
to export. Builder methods override their environment defaults.

These controls apply to the `FISpan` API and context attributes added by `FITracer`.
Writing directly to its underlying
`Activity`, or using another instrumentation library, bypasses this masking.

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
