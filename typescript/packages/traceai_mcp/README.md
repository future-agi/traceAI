# MCP tracing

`@traceai/mcp` connects application traces across MCP client/server requests. By default it only injects the active OpenTelemetry context into request `params._meta` and restores it while the receiving SDK dispatches the request. It does **not** replace your tracing provider. For the npm `fastmcp` framework, you can explicitly enable server-operation spans with `instrumentFastMCP()`.

## Install

Install the package alongside the MCP SDK your application uses:

```bash
npm install @traceai/mcp
```

Use `@modelcontextprotocol/sdk` 1.x (1.10.2 or later), or `@modelcontextprotocol/server` 2.4.x. The SDK peers are optional so split-package applications do not have to install the legacy SDK. Initialize your OpenTelemetry provider, async context manager and W3C trace-context propagator in **both** processes before handling requests. For Future AGI, use [`register()` from `@traceai/fi-core`](https://docs.futureagi.com/docs/integrations/traceai/mcp) only after installing a release that configures all three; version 1.1.0 does not. With 1.1.0, configure the context manager and W3C propagator separately, or use a standard OpenTelemetry Node provider.

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
- Without `instrumentFastMCP()`, no execution spans are added. Keep argument/result capture and privacy controls in the application or framework that creates those spans.

If a server span appears in a separate trace, instrument both endpoints, start a client application span before making the request, and ensure both providers register a W3C propagator and an async context manager. A server that only propagates context and never creates spans has nothing to export by itself.

## Trace npm FastMCP operations

Use Node 22.23.4 or later and npm `fastmcp` 3.35.x or 4.x. This is not Python FastMCP. FastMCP supports legacy, handshake-based MCP revisions; this adapter does not add newer protocol support or instrument ViteMCP.

Initialize your standard OpenTelemetry provider before starting the server. Then opt in on the same instrumentation instance you use for MCP propagation:

```ts
import * as fastmcp from "fastmcp";
import { MCPInstrumentation } from "@traceai/mcp";

const instrumentation = new MCPInstrumentation({ instrumentationConfig: { enabled: false } });
const supported = instrumentation.instrumentFastMCP(fastmcp, {
  version: "4.23.8", // Match the version installed in your application.
});
if (!supported) throw new Error("Unsupported FastMCP version or module shape");

const server = new fastmcp.FastMCP({ name: "inventory", version: "1.0.0" });
// Register tools, resources and prompts normally, then server.start(...).
```

This explicit module hook works for CommonJS and ESM and covers transient stateless HTTP sessions. Install it **before** any `start()`/`connect()` calls. Unsupported versions, prereleases and module shapes return `false` without changing application execution. One instrumentation instance should own a server's lifecycle. Call `disable()` to stop tracing; reapply explicit hooks before creating new connections to resume.

Each incoming operation has one `SERVER` span named `fastmcp <method>`. Tool calls use `fi.span.kind=TOOL` and `tool.name`; other methods use `CHAIN`. Resource reads record a SHA-256 URI identifier rather than disclosing the URI. Spans inside your handlers inherit the operation as their parent. Discovery, initialization and ping are metadata-only. Notifications never create operation spans. Reverse sampling and elicitation use context propagation; instrument the client's transport and the actual model SDK to trace model work. This package does not infer token usage or cost from tool output.

HTTP session IDs are recorded as `mcp.session.id` after initialization; absent IDs on stdio/stateless transports stay absent. Set `sessionId` explicitly only when you have a separate end-user conversation ID; it becomes `session.id`.

Arguments and results are **off by default**. To enable bounded JSON capture, pass `captureContent: { input: true, output: true, maxLength: 4096 }`. This captures tool/prompt arguments and successful tool/resource/prompt results, not discovery payloads. `FI_HIDE_INPUTS`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_INPUT_MESSAGES`, and their `OUTPUT` equivalents override capture when set to `true`. Headers, `_meta`, credential-named fields, image/audio/binary blocks, error messages, error results and stacks are never captured, including when `FI_HIDE_ERROR_DETAILS` is unset or false. Application-defined text may still contain sensitive data when you opt in; enable capture only for data you intend to export. Values exceeding the byte/work/depth limit are omitted rather than emitted as invalid JSON.

Requests end after response delivery or observed cancellation/close/failure. Defaults bound unfinished spans to five minutes and 1024 in-flight requests per transport; configure `maxDurationMs` (maximum one hour) and `maxPendingRequests` (maximum 10000) if needed. Bounds affect telemetry only, not tool execution. In stateless HTTP, a cancellation POST can reach a different FastMCP server instance; the original operation retains its actual server outcome rather than being marked cancelled solely because the client stopped waiting.
