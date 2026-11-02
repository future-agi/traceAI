import { context, propagation, ROOT_CONTEXT, trace } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { W3CTraceContextPropagator } from "@opentelemetry/core";
import { createServer } from "node:http";
import { AddressInfo } from "node:net";
import { MCPInstrumentation } from "../mcp";

const traceId = "11111111111111111111111111111111";
const traceparent = `00-${traceId}-2222222222222222-01`;

it.each(["mcp-sdk-floor", "@modelcontextprotocol/sdk"])(
  "preserves Node HTTP adapter callback delegation on %s",
  async (root) => {
    const manager = new AsyncLocalStorageContextManager().enable();
    context.setGlobalContextManager(manager);
    propagation.setGlobalPropagator(new W3CTraceContextPropagator());
    const module = require(`${root}/server/streamableHttp.js`);
    const instrumentation = new MCPInstrumentation({
      instrumentationConfig: { enabled: false },
    });
    instrumentation.manuallyInstrument({ serverStreamableHTTPModule: module });
    const transport = new module.StreamableHTTPServerTransport({
      sessionIdGenerator: undefined,
      enableJsonResponse: true,
    });
    let observed: string | undefined;
    const onmessage = (message: { id: number }) => {
      observed = trace.getSpanContext(context.active())?.traceId;
      void transport.send({ jsonrpc: "2.0", id: message.id, result: {} });
    };
    transport.onmessage = onmessage;
    const hadOwnCallback = Object.hasOwn(transport, "onmessage");
    const server = createServer((request, response) => {
      context.with(ROOT_CONTEXT, () => {
        void transport.handleRequest(request, response).catch(() => {
          response.statusCode = 500;
          response.end();
        });
      });
    });
    try {
      await transport.start();
      await new Promise<void>((resolve) =>
        server.listen(0, "127.0.0.1", resolve),
      );
      const response = await fetch(
        `http://127.0.0.1:${(server.address() as AddressInfo).port}/mcp`,
        {
          method: "POST",
          headers: {
            "content-type": "application/json",
            accept: "application/json, text/event-stream",
          },
          body: JSON.stringify({
            jsonrpc: "2.0",
            id: 1,
            method: "initialize",
            params: {
              protocolVersion: "2025-03-26",
              capabilities: {},
              clientInfo: { name: "offline", version: "1" },
              _meta: { traceparent },
            },
          }),
        },
      );
      expect(response.status).toBe(200);
      await response.text();
      expect(observed).toBe(traceId);
      expect(Object.hasOwn(transport, "onmessage")).toBe(hadOwnCallback);
    } finally {
      await transport.close();
      instrumentation.disable();
      server.closeAllConnections();
      await new Promise<void>((resolve) => server.close(() => resolve()));
      manager.disable();
      context.disable();
      propagation.disable();
    }
    expect(transport.onmessage).toBe(onmessage);
  },
);
