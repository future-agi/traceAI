# MCP context propagation

`@traceai/mcp` connects application traces across MCP client/server requests. It injects the active OpenTelemetry context into request `params._meta` and restores it while the receiving SDK dispatches the request. It does **not** create spans, capture arguments/results, or replace your tracing provider.

## Install

Install the package alongside the MCP SDK your application uses:

```bash
npm install @traceai/mcp
```

Use `@modelcontextprotocol/sdk` 1.x (1.10.2 or later), or `@modelcontextprotocol/server` 2.4.x. The SDK peers are optional so split-package applications do not have to install the legacy SDK. Initialize your OpenTelemetry provider, async context manager and W3C trace-context propagator in **both** processes before handling requests. For Future AGI, use [`register()` from `@traceai/fi-core`](https://docs.futureagi.com/docs/integrations/traceai/mcp).

## Instrument a server

Add instrumentation before the server's existing `connect()` call. This works with the public `Server` and `McpServer` interfaces, independent of transport, and covers handlers registered before or after instrumentation:

```ts
import { MCPInstrumentation } from "@traceai/mcp";
import { McpServer, WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/server";

const instrumentation = new MCPInstrumentation();
const server = new McpServer({ name: "inventory", version: "1.0.0" });
instrumentation.instrumentServer(server);

// Register your tools and resources on server as usual.
const transport = new WebStandardStreamableHTTPServerTransport({
  sessionIdGenerator: undefined,
});
await server.connect(transport);
// Pass incoming Web Request objects to transport.handleRequest(request)
// using your HTTP framework's normal routing and session lifecycle.
```

In a framework that supplies the server, call `instrumentation.instrumentServer(server)` in its server-initialization callback before it connects. This includes `mcp-handler`; no copied handler wrapper is needed.

For an MCP client or a custom transport, call `instrumentation.instrumentTransport(transport)` before `client.connect(transport)` or `server.connect(transport)`. Create application spans around client requests, or use your agent framework's existing tracing. Application spans created inside server handlers inherit the remote parent.

## Keep existing transport setup

The original six `manuallyInstrument()` options remain supported for legacy SSE, stdio and streamable HTTP transports. Use `.js` in SDK import paths:

```ts
import { MCPInstrumentation } from "@traceai/mcp";
import * as clientStdioModule from "@modelcontextprotocol/sdk/client/stdio.js";
import * as serverStdioModule from "@modelcontextprotocol/sdk/server/stdio.js";

const instrumentation = new MCPInstrumentation();
instrumentation.manuallyInstrument({ clientStdioModule, serverStdioModule });
```

For direct web-standard transports in the legacy SDK, import their own module rather than the Node HTTP adapter:

```ts
import * as serverWebStandardStreamableHTTPModule from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";

instrumentation.manuallyInstrument({ serverWebStandardStreamableHTTPModule });
```

For split-package servers, you can instrument the exported classes instead of individual instances:

```ts
import * as serverModule from "@modelcontextprotocol/server";

instrumentation.manuallyInstrument({ serverModule });
```

Explicit instrumentation is the portable route for ESM, preloaded modules and bundled applications. CommonJS applications may also enable instrumentation before requiring `@modelcontextprotocol/server` to use its module hook.

## Context and lifecycle

- Only JSON-RPC **requests** carry propagated context. Notifications (including progress and cancellation) and responses are forwarded unchanged.
- Existing `_meta` fields and send options are preserved. Request objects are not mutated.
- Instrumenting the same transport through both server and transport entry points does not wrap it twice.
- `instrumentation.disable()` restores module methods and callbacks on active transports. Closed transports release their instrumentation state. Reapply explicit instrumentation before the next connection if you want to turn a manually instrumented server back on.
- No execution spans are added, so an agent framework's tool spans are not duplicated. Keep argument/result capture and privacy controls in the application or framework that creates spans.

If a server span appears in a separate trace, instrument both endpoints, start a client application span before making the request, and ensure both providers register a W3C propagator and an async context manager. A server that only propagates context and never creates spans has nothing to export by itself.
