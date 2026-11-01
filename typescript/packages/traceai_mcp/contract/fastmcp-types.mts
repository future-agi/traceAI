import * as fastmcp from "fastmcp";
import { MCPInstrumentation, type FastMCPOptions } from "@traceai/mcp";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";

const options: FastMCPOptions = {
  version: "4.23.8",
  captureContent: { input: false, output: false },
  maxPendingRequests: 100,
};
const instrumentation = new MCPInstrumentation({
  instrumentationConfig: { enabled: false },
});
instrumentation.setTracerProvider(new NodeTracerProvider());
const supported: boolean = instrumentation.instrumentFastMCP(fastmcp, options);
void supported;
instrumentation.disable();
