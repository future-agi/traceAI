# MCP OpenTelemetry integration

Connect trace context across Python MCP client and server transports. `traceAI-mcp` propagates context; it does not create spans or configure a tracer provider.

## Native MCP 2.x tracing

For MCP 2.3 and later in the 2.x series, prefer the SDK's native OpenTelemetry support. It creates client request and server operation spans, including tool names, and propagates W3C trace context. Do not enable `MCPInstrumentor` alongside this native route.

Follow the [Python MCP tracing guide](https://docs.futureagi.com/docs/integrations/traceai/mcp-python) for standard OpenTelemetry configuration and a runnable calculator client/server. No OpenAI API key, agent framework, or copied instrumentation wrapper is required. Configure a provider/exporter in each process; a client cannot export a remote server's spans.

Native MCP error spans can contain exception text and stack traces. The `FI_HIDE_*` settings do not control native MCP telemetry. Use safe tool names and error messages, and apply your Collector's redaction policy when needed.

## MCP 1.x context propagation

Use Python 3.11–3.13. Install the published package and the MCP 1.x SDK:

```bash
pip install "traceAI-mcp>=0.1.3" "mcp>=1.30.0,<2"
```

Keep your existing application, agent, or model instrumentation to create spans. Configure its provider/exporter in both client and server processes. Enable MCP propagation in each process before importing transport functions:

```python
from traceai_mcp import MCPInstrumentor

MCPInstrumentor().instrument()
```

The instrumentor connects existing spans; installing it alone will not create a request or tool span tree. It requires `mcp>=1.30.0`; new MCP 2.x setups should use the native route instead.

### Upgrading older MCP 1.x applications

Upgrade the MCP SDK to at least 1.30.0 in both client and server environments. MCP 1.8.1 can close the session when a request is cancelled, preventing subsequent requests from completing. Pinning Pydantic resolves an import incompatibility in that older SDK, but does not fix cancellation. SDK versions below 1.30.0 are no longer supported by this instrumentor.

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

## Tool and resource media (attachments)

`MCPInstrumentor` propagates context only and never captures media. To upload
the images, audio and inline resource blobs a server returns, wrap the client
session:

```python
from traceai_mcp import wrap_mcp_client

session = wrap_mcp_client(session, tracer_provider=trace_provider)
result = await session.call_tool("screenshot", {})   # unchanged SDK result
```

Each `call_tool` / `read_resource` gets an `mcp.tools/call` /
`mcp.resources/read` CLIENT span. `ImageContent.data`, `AudioContent.data`
and `BlobResourceContents.blob` (in an `EmbeddedResource` or a
`resources/read` result) are uploaded as `tool_result` occurrences: at most 64
parts and 256 KiB of decoded bytes per result. `resource_link` blocks,
resource URIs and `structuredContent` are never read. A MIME of another
modality than the block type, or outside the attachment MIME list, uploads
nothing. `attachments=` overrides the provider's manager; `trace_config=` and
`attachment_capture=` only add restrictions. Wrapping a wrapped session
returns it unchanged. `capture_mcp_result(manager, span, result, operation)`
is the same capture for an existing span.

Attachments are off unless the provider's attachment manager is enabled
(`fi_instrumentation.register(..., attachment_config=AttachmentConfig(enabled=True))`
or `traceai.init(attachment_config=...)`). Tool media is tool output and
potential model input, so every input and output hide flag
(`hide_inputs`, `hide_outputs` and their message/text variants),
`hide_input_images` for images, and `pii_redaction` deny the upload; they
cannot be relaxed by the server or the core. Only typed inline bytes are
read, and only after the manager admitted them; URLs, file IDs, resource URIs
and local paths are never fetched or opened. Call the manager's `flush()`
before a short process exits. Upload completion means "transferred for
validation"; playback needs an authorized occurrence through the media
gateway.
