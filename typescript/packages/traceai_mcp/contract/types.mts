// Compile in the clean tarball install with only the split SDK present.
import { MCPInstrumentation } from "@traceai/mcp";
import * as serverModule from "@modelcontextprotocol/server";

const instrumentation = new MCPInstrumentation();
const lowLevel = new serverModule.Server({ name: "types", version: "1" });
const highLevel = new serverModule.McpServer({ name: "types", version: "1" });
const transport = new serverModule.WebStandardStreamableHTTPServerTransport();
instrumentation.instrumentServer(lowLevel);
instrumentation.instrumentServer(highLevel);
instrumentation.instrumentTransport(transport);
instrumentation.manuallyInstrument({ serverModule });
instrumentation.manuallyInstrument({
  serverWebStandardStreamableHTTPModule: serverModule,
});
instrumentation.disable();
