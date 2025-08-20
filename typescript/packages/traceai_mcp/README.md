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