# MCP OpenTelemetry integration

Connect trace context across Python MCP client and server transports. `traceAI-mcp` propagates context; it does not create spans or configure a tracer provider.

## Native MCP 2.x tracing

For MCP 2.3 and later in the 2.x series, prefer the SDK's native OpenTelemetry support. It creates client request and server operation spans, including tool names, and propagates W3C trace context. Do not enable `MCPInstrumentor` alongside this native route.

Follow the [Python MCP tracing guide](https://docs.futureagi.com/docs/integrations/traceai/mcp-python) for standard OpenTelemetry configuration and a runnable calculator client/server. No OpenAI API key, agent framework, or copied instrumentation wrapper is required. Configure a provider/exporter in each process; a client cannot export a remote server's spans.

Native MCP error spans can contain exception text and stack traces. The `FI_HIDE_*` settings do not control native MCP telemetry. Use safe tool names and error messages, and apply your Collector's redaction policy when needed.

## MCP 1.x context propagation

Use Python 3.11–3.13. Install the published package and the MCP 1.x SDK:

```bash
pip install "traceAI-mcp>=0.1.3" "mcp>=1.8.1,<2"
```

Keep your existing application, agent, or model instrumentation to create spans. Configure its provider/exporter in both client and server processes. Enable MCP propagation in each process before importing transport functions:

```python
from traceai_mcp import MCPInstrumentor

MCPInstrumentor().instrument()
```

The instrumentor connects existing spans; installing it alone will not create a request or tool span tree. Its package dependency remains `mcp>=1.8.1` for compatibility with existing applications, but new MCP 2.x setups should use the native route instead.

### MCP 1.8.1 dependency compatibility

MCP 1.8.1 uses a private Pydantic API that is absent in newer Pydantic releases. If you must stay on MCP 1.8.1, pin its compatible Pydantic dependency:

```bash
pip install "traceAI-mcp>=0.1.3" "mcp==1.8.1" "pydantic==2.11.10"
```

This pin applies only to MCP 1.8.1; do not impose it on newer SDK versions.

## Instrumentation lifecycle

`uninstrument()` disables this instrumentor's per-message propagation on
already-instrumented open streams as well as on newly opened transports; it
does not close sessions or remove wrappers installed by other libraries.
Reinstalling re-enables the existing instrumented streams and instruments new
transports exactly once. Sessions created while instrumentation is off are
not retroactively wrapped; reopen them after reinstall to instrument them.
Queued server messages remain intact across transitions; context captured while
enabled is attached only if the reader is enabled when it consumes the item.
An already-running request retains the context under which it started. Native
MCP SDK tracing remains independent and can continue when this instrumentor is
off, particularly with MCP 2.x.
