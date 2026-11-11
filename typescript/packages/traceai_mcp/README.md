This module provides automatic instrumentation for the MCP Typescript SDK. which may be used in conjunction with @opentelemetry/sdk-trace-node.

## Installation

```bash
npm install --save @traceai/mcp
```

## Usage

For example, if using stdio transport,

```ts
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { MCPInstrumentation } from "@traceai/mcp";
import * as MCPClientStdioModule from "@modelcontextprotocol/sdk/client/stdio";
import * as MCPServerStdioModule from "@modelcontextprotocol/sdk/server/stdio";

const provider = new NodeTracerProvider();
provider.register();

const mcpInstrumentation = new MCPInstrumentation();
// MCP must be manually instrumented as it doesn't have a traditional module structure
mcpInstrumentation.manuallyInstrument({
  clientStdioModule: MCPClientStdioModule,
  serverStdioModule: MCPServerStdioModule,
});
```

For more information on OpenTelemetry Node.js SDK, see the OpenTelemetry Node.js SDK documentation.

## Typed media capture API

`wrapMCPClient(client, config)` observes `callTool()` image/audio/embedded-resource
blocks and `readResource()` blob contents. It creates a span for each operation
and uses the provider's shared attachment manager; the transport-only
`MCPInstrumentation` above propagates context but does not capture media.

This API requires an attachment-enabled `@traceai/fi-core` provider and an
attachment-capable service. Attachments remain off unless explicitly enabled
on that provider. Pass the same `tracerProvider` to `wrapMCPClient`; do not create
an uploader per client. An explicitly supplied `attachments` manager is also
accepted for custom provider setups.

| Configuration | Effect |
| --- | --- |
| `tracerProvider` | Provider that creates the operation span and owns `attachments` |
| `attachments` | Optional shared manager override |
| `traceConfig` | Additional hide/PII restrictions; cannot loosen manager policy |
| `attachmentCapture` | Additional `captureContent`, `recordInputs`, `recordOutputs`, `muted` and `temporaryChat` restrictions |

Tool media is both tool output and potential model input. Input restrictions,
including `hideInputImages`, therefore apply even when a screenshot first
appears in a tool response. Required PII redaction without a qualified media
sanitizer denies raw media capture. Original media may contain embedded metadata.

Capture inspects only typed inline base64, at most 64 parts and 256 KiB of decoded
bytes per response. Larger parts are not uploaded. Resource links, URLs, local
paths and arbitrary `structuredContent` are not fetched or scraped. Capture
never creates recordings or advances streams. The original SDK promise,
response, error and cancellation signal are preserved.

Call the provider's `forceFlush()` or the shared manager's `flush()` before a
short process exits. Upload completion means transferred for validation, not
permission to play the content. Playback requires an authorized live occurrence
through the media gateway. Do not wrap the same underlying client multiple
times or enable overlapping media observers for the same operation.
