import {
  context,
  propagation,
  ROOT_CONTEXT,
  trace,
  TraceFlags,
} from "@opentelemetry/api";
import { W3CTraceContextPropagator } from "@opentelemetry/core";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { createServer, Server as HttpServer } from "node:http";
import { AddressInfo } from "node:net";
import { MCPInstrumentation, isPatched } from "../mcp";

const sdkRoutes = [
  { name: "legacy floor 1.10.2", root: "mcp-sdk-floor", split: false },
  { name: "legacy current", root: "@modelcontextprotocol/sdk", split: false },
  { name: "split server 2.4.0", root: "@modelcontextprotocol", split: true },
];
const traceId = "11111111111111111111111111111111";
const parentId = "2222222222222222";
const traceparent = `00-${traceId}-${parentId}-01`;
const parent = (id = traceId) =>
  trace.setSpanContext(ROOT_CONTEXT, {
    traceId: id,
    spanId: parentId,
    traceFlags: TraceFlags.SAMPLED,
  });
const activeId = () => trace.getSpanContext(context.active())?.traceId;
const tick = () => new Promise<void>((resolve) => setImmediate(resolve));

type Message = {
  jsonrpc: string;
  id?: number | string;
  method?: string;
  params?: { _meta?: Record<string, unknown>; [key: string]: unknown };
  [key: string]: unknown;
};
// A real SDK protocol pair with a serialization boundary and independent receive
// contexts, not an in-process transport that accidentally inherits the sender.
class WireTransport {
  peer!: WireTransport;
  onmessage?: (message: Message) => void;
  onclose?: () => void;
  sent: Message[] = [];
  async start() {}
  async send(message: Message) {
    this.sent.push(message);
    const copy = JSON.parse(JSON.stringify(message));
    queueMicrotask(() =>
      context.with(ROOT_CONTEXT, () => this.peer.onmessage?.(copy)),
    );
  }
  async close() {
    this.onclose?.();
  }
  static pair(): [WireTransport, WireTransport] {
    const a = new WireTransport();
    const b = new WireTransport();
    a.peer = b;
    b.peer = a;
    return [a, b];
  }
}

let instrumentation: MCPInstrumentation;
let provider: NodeTracerProvider;
let collector: HttpServer;
let manager: AsyncLocalStorageContextManager;
const payloads: string[] = [];

beforeAll(async () => {
  collector = createServer((request, response) => {
    let body = "";
    request.setEncoding("utf8");
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      payloads.push(body);
      response.setHeader("Content-Type", "application/json");
      response.end("{}");
    });
  });
  await new Promise<void>((resolve) =>
    collector.listen(0, "127.0.0.1", resolve),
  );
  const port = (collector.address() as AddressInfo).port;
  manager = new AsyncLocalStorageContextManager().enable();
  provider = new NodeTracerProvider();
  provider.addSpanProcessor(
    new SimpleSpanProcessor(
      new OTLPTraceExporter({ url: `http://127.0.0.1:${port}/v1/traces` }),
    ),
  );
  provider.register({
    contextManager: manager,
    propagator: new W3CTraceContextPropagator(),
  });
});
beforeEach(() => {
  payloads.length = 0;
  instrumentation = new MCPInstrumentation({
    instrumentationConfig: { enabled: false },
  });
});
afterEach(async () => {
  instrumentation.disable();
  await provider.forceFlush();
});
afterAll(async () => {
  await provider.shutdown();
  await new Promise<void>((resolve, reject) =>
    collector.close((error) => (error ? reject(error) : resolve())),
  );
  manager.disable();
  context.disable();
  propagation.disable();
  trace.disable();
});

function spans() {
  return payloads.flatMap((body) =>
    JSON.parse(body).resourceSpans.flatMap(
      (resource: { scopeSpans: { spans: unknown[] }[] }) =>
        resource.scopeSpans.flatMap((scope) => scope.spans),
    ),
  );
}

for (const route of sdkRoutes) {
  describe(route.name, () => {
    const { Server } = require(
      route.split ? `${route.root}/server` : `${route.root}/server/index.js`,
    );
    const { Client } = require(
      route.split ? `${route.root}/client` : `${route.root}/client/index.js`,
    );
    const schemas = require(
      route.split ? `${route.root}/core` : `${route.root}/types.js`,
    );
    const serverModule = { Server };
    let server: InstanceType<typeof Server>;
    let client: InstanceType<typeof Client>;
    let clientTransport: WireTransport;
    let serverTransport: WireTransport;

    beforeEach(() => {
      server = new Server(
        { name: "offline-server", version: "1" },
        { capabilities: { tools: {} } },
      );
      client = new Client(
        { name: "offline-client", version: "1" },
        { capabilities: { roots: { listChanged: true } } },
      );
      [clientTransport, serverTransport] = WireTransport.pair();
    });
    afterEach(async () => {
      await client.close();
      await server.close();
    });
    async function connect() {
      instrumentation.manuallyInstrument({ serverModule });
      instrumentation.instrumentTransport(clientTransport);
      await server.connect(serverTransport);
      await client.connect(clientTransport);
    }

    it("propagates concurrent requests to existing and replaced handlers, exporting only application spans", async () => {
      const observed: (string | undefined)[] = [];
      const handler = async () => {
        observed.push(activeId());
        await tick();
        observed.push(activeId());
        const span = trace
          .getTracer("application")
          .startSpan("application.tool");
        span.end();
        return { content: [{ type: "text", text: "private-tool-result" }] };
      };
      server.setRequestHandler(
        route.split ? "tools/call" : schemas.CallToolRequestSchema,
        handler,
      );
      await connect();
      const otherId = "33333333333333333333333333333333";
      await Promise.all([
        context.with(parent(), () =>
          client.callTool({
            name: "first",
            arguments: { secret: "private-tool-input" },
          }),
        ),
        context.with(parent(otherId), () =>
          client.callTool({ name: "second" }),
        ),
      ]);
      server.setRequestHandler(
        route.split ? "tools/call" : schemas.CallToolRequestSchema,
        handler,
      );
      await context.with(parent(), () =>
        client.callTool({ name: "replacement" }),
      );
      expect(observed.filter((id) => id === traceId)).toHaveLength(4);
      expect(observed.filter((id) => id === otherId)).toHaveLength(2);
      expect(activeId()).toBeUndefined();
      await provider.forceFlush();
      const exported = spans();
      expect(exported).toHaveLength(3);
      expect(
        exported.every(
          (span: { name: string; parentSpanId: string }) =>
            span.name === "application.tool" && span.parentSpanId === parentId,
        ),
      ).toBe(true);
      expect(
        new Set(exported.map((span: { traceId: string }) => span.traceId)),
      ).toEqual(new Set([traceId, otherId]));
      expect(payloads.join("")).not.toContain("private-tool");
    });

    it("leaves notifications untouched and preserves cancellation signals", async () => {
      let notify!: () => void;
      const notified = new Promise<void>((resolve) => {
        notify = resolve;
      });
      let notificationParent: string | undefined;
      server.setNotificationHandler(
        route.split
          ? "notifications/roots/list_changed"
          : schemas.RootsListChangedNotificationSchema,
        () => {
          notificationParent = activeId();
          notify();
        },
      );
      let entered!: () => void;
      const started = new Promise<void>((resolve) => {
        entered = resolve;
      });
      let cancelled!: () => void;
      const aborted = new Promise<void>((resolve) => {
        cancelled = resolve;
      });
      server.setRequestHandler(
        route.split ? "tools/call" : schemas.CallToolRequestSchema,
        async (
          _request: unknown,
          extra: { signal: AbortSignal; mcpReq: { signal: AbortSignal } },
        ) => {
          entered();
          const signal = route.split ? extra.mcpReq.signal : extra.signal;
          await new Promise<void>((resolve) =>
            signal.addEventListener(
              "abort",
              () => {
                cancelled();
                resolve();
              },
              { once: true },
            ),
          );
          return { content: [] };
        },
      );
      await connect();
      await context.with(parent(), () =>
        client.notification({
          method: "notifications/roots/list_changed",
          params: { _meta: { traceparent } },
        }),
      );
      await notified;
      expect(notificationParent).toBeUndefined();
      const controller = new AbortController();
      const request = { method: "tools/call", params: { name: "cancel" } };
      const call = context.with(parent(), () =>
        route.split
          ? client.request(request, { signal: controller.signal })
          : client.request(request, schemas.CallToolResultSchema, {
              signal: controller.signal,
            }),
      );
      const rejected = expect(call).rejects.toBeDefined();
      await started;
      controller.abort();
      await rejected;
      await aborted;
      const cancellation = clientTransport.sent.find(
        (message) => message.method === "notifications/cancelled",
      );
      expect(cancellation).toBeDefined();
      expect(cancellation?.params?._meta).toBeUndefined();
      expect(spans()).toHaveLength(0);
    });

    it("keeps errors and return values intact, then stops propagation on disable", async () => {
      server.setRequestHandler(
        route.split ? "tools/call" : schemas.CallToolRequestSchema,
        () => {
          throw new Error("expected-offline-error");
        },
      );
      await connect();
      await expect(
        context.with(parent(), () => client.callTool({ name: "fail" })),
      ).rejects.toThrow("expected-offline-error");
      server.setRequestHandler(
        route.split ? "tools/call" : schemas.CallToolRequestSchema,
        () => ({ content: [{ type: "text", text: activeId() ?? "none" }] }),
      );
      instrumentation.disable();
      const result = await context.with(parent(), () =>
        client.callTool({ name: "untraced" }),
      );
      expect(result.content[0].text).toBe("none");
      expect(spans()).toHaveLength(0);
    });
  });
}

for (const [name, webModule, split] of [
  [
    "direct legacy web-standard",
    require("@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js"),
    false,
  ],
  ["direct split web-standard", require("@modelcontextprotocol/server"), true],
] as const) {
  it(`${name} extracts the parent on the real HTTP transport`, async () => {
    if (split) instrumentation.manuallyInstrument({ serverModule: webModule });
    else
      instrumentation.manuallyInstrument({
        serverWebStandardStreamableHTTPModule: webModule,
      });
    const transport = new webModule.WebStandardStreamableHTTPServerTransport({
      sessionIdGenerator: undefined,
      enableJsonResponse: true,
    });
    let observed: string | undefined;
    transport.onmessage = (message: Message) => {
      observed = activeId();
      trace.getTracer("application").startSpan("web.handler").end();
      void transport.send({ jsonrpc: "2.0", id: message.id, result: {} });
    };
    await transport.start();
    try {
      const response = await context.with(ROOT_CONTEXT, () =>
        transport.handleRequest(
          new Request("http://localhost/mcp", {
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
          }),
        ),
      );
      expect(response.status).toBe(200);
      expect(observed).toBe(traceId);
      await provider.forceFlush();
      expect(spans()).toEqual([
        expect.objectContaining({
          name: "web.handler",
          traceId,
          parentSpanId: parentId,
        }),
      ]);
    } finally {
      await transport.close();
    }
  });
}

it("preserves frozen messages, metadata, send options and promise identity without double wrapping", async () => {
  const promise = Promise.resolve();
  const send = jest.fn((_message: unknown, _options?: unknown) => promise);
  const transport = { start: async () => {}, close: async () => {}, send };
  instrumentation.instrumentTransport(transport);
  const wrapped = transport.send;
  instrumentation.instrumentTransport(transport);
  expect(transport.send).toBe(wrapped);
  const message = Object.freeze({
    jsonrpc: "2.0",
    id: 0,
    method: "tools/call",
    params: Object.freeze({
      _meta: Object.freeze({ custom: "keep" }),
      name: "tool",
    }),
  });
  const options = { relatedRequestId: 42 };
  const result = context.with(parent(), () =>
    Reflect.apply(transport.send, transport, [message, options]),
  );
  expect(result).toBe(promise);
  expect(send).toHaveBeenCalledWith(
    expect.objectContaining({
      params: { name: "tool", _meta: { custom: "keep", traceparent } },
    }),
    options,
  );
  expect(message.params._meta).toEqual({ custom: "keep" });
  const notification = { jsonrpc: "2.0", method: "notifications/progress" };
  Reflect.apply(transport.send, transport, [notification]);
  expect(send.mock.calls[1][0]).toBe(notification);
  await transport.close();
  expect(transport.send).toBe(send);
});

it.each([null, "invalid", [], { _meta: "invalid" }, { _meta: null }])(
  "forwards malformed params unchanged: %p",
  async (params) => {
    const send = jest.fn(async (_message: unknown) => {});
    const transport = { start: async () => {}, close: async () => {}, send };
    instrumentation.instrumentTransport(transport);
    const message = { jsonrpc: "2.0", id: 1, method: "tools/call", params };
    await context.with(parent(), () => transport.send(message));
    expect(send.mock.calls[0][0]).toBe(message);
  },
);

it.each([false, true])(
  "cleans up failed connections without changing rejection identity (sync=%s)",
  async (synchronous) => {
    const transport = new WireTransport();
    const error = new Error("connect failed");
    const connect = jest.fn(() => {
      if (synchronous) throw error;
      return Promise.reject(error);
    });
    const server = { connect, close: async () => {} };
    instrumentation.instrumentServer(server);
    if (synchronous)
      expect(() => Reflect.apply(server.connect, server, [transport])).toThrow(
        error,
      );
    else
      await expect(
        Reflect.apply(server.connect, server, [transport]),
      ).rejects.toBe(error);
    expect(server.connect).toBe(connect);
    expect(Object.hasOwn(transport, "send")).toBe(false);
  },
);

it("restores inherited methods and callbacks, and does not overwrite customer replacements", async () => {
  const transport = new WireTransport();
  const callback = jest.fn();
  transport.onmessage = callback;
  instrumentation.instrumentTransport(transport);
  await transport.start();
  const customer = jest.fn();
  transport.onmessage = customer;
  instrumentation.disable();
  expect(transport.onmessage).toBe(customer);
  expect(Object.hasOwn(transport, "send")).toBe(false);
  expect(Object.hasOwn(transport, "start")).toBe(false);
  expect(Object.hasOwn(transport, "close")).toBe(false);
});

it("supports a framework-owned McpServer and reapplication after disable", async () => {
  const { McpServer } = require("@modelcontextprotocol/sdk/server/mcp.js");
  const server = new McpServer({ name: "framework-owned", version: "1" });
  const connect = server.connect;
  instrumentation.instrumentServer(server);
  expect(server.connect).not.toBe(connect);
  instrumentation.disable();
  expect(server.connect).toBe(connect);
  instrumentation.instrumentServer(server);
  expect(server.connect).not.toBe(connect);
  await server.close();
  expect(server.connect).toBe(connect);
});

it.each([false, true])(
  "supports per-request server factories (modern=%s) without retaining closed servers",
  async (modern) => {
    const sdk = require("@modelcontextprotocol/server");
    const servers: { connect: unknown; originalConnect: unknown }[] = [];
    const handler = sdk.createMcpHandler(() => {
      const server = new sdk.McpServer({ name: "factory", version: "1" });
      const originalConnect = server.connect;
      instrumentation.instrumentServer(server);
      servers.push(Object.assign(server, { originalConnect }));
      server.registerTool(
        "echo",
        { description: "Echo", inputSchema: {} },
        async () => {
          await tick();
          trace.getTracer("application").startSpan("factory.tool").end();
          return { content: [{ type: "text", text: activeId() ?? "none" }] };
        },
      );
      return server;
    });
    try {
      for (let id = 1; id <= 3; id++) {
        const meta = modern
          ? {
              traceparent,
              [sdk.PROTOCOL_VERSION_META_KEY]: "2026-07-28",
              [sdk.CLIENT_CAPABILITIES_META_KEY]: {},
            }
          : { traceparent };
        const headers: Record<string, string> = modern
          ? {
              "mcp-protocol-version": "2026-07-28",
              "mcp-method": "tools/call",
              "mcp-name": "echo",
            }
          : {};
        const response = await context.with(ROOT_CONTEXT, () =>
          handler.fetch(
            new Request("http://localhost/mcp", {
              method: "POST",
              headers: {
                "content-type": "application/json",
                accept: "application/json, text/event-stream",
                ...headers,
              },
              body: JSON.stringify({
                jsonrpc: "2.0",
                id,
                method: "tools/call",
                params: { name: "echo", arguments: {}, _meta: meta },
              }),
            }),
          ),
        );
        const text = await response.text();
        expect(response.status).toBe(200);
        expect(text).toContain(traceId);
      }
      await tick();
      for (const server of servers)
        expect(server.connect).toBe(server.originalConnect);
      await provider.forceFlush();
      expect(spans()).toHaveLength(3);
      expect(
        spans().every(
          (span: { traceId: string; parentSpanId: string }) =>
            span.traceId === traceId && span.parentSpanId === parentId,
        ),
      ).toBe(true);
    } finally {
      await handler.close();
    }
  },
);

for (const root of ["mcp-sdk-floor", "@modelcontextprotocol/sdk"]) {
  it(`preserves all six legacy module entry points on ${root}`, async () => {
    const paths = [
      ["clientSSEModule", "client/sse", "SSEClientTransport"],
      ["serverSSEModule", "server/sse", "SSEServerTransport"],
      ["clientStdioModule", "client/stdio", "StdioClientTransport"],
      ["serverStdioModule", "server/stdio", "StdioServerTransport"],
      [
        "clientStreamableHTTPModule",
        "client/streamableHttp",
        "StreamableHTTPClientTransport",
      ],
      [
        "serverStreamableHTTPModule",
        "server/streamableHttp",
        "StreamableHTTPServerTransport",
      ],
    ];
    const originals = paths.map(([key, path, name]) => {
      const module = require(`${root}/${path}.js`);
      const prototype = module[name].prototype;
      const send = prototype.send;
      const start = prototype.start;
      instrumentation.manuallyInstrument({ [key]: module });
      expect(prototype.send).not.toBe(send);
      expect(prototype.start).not.toBe(start);
      expect(
        isPatched(
          `@modelcontextprotocol/sdk/${path}` as Parameters<
            typeof isPatched
          >[0],
        ),
      ).toBe(true);
      return { prototype, send, start, path };
    });
    instrumentation.disable();
    for (const { prototype, send, start, path } of originals) {
      expect(prototype.send).toBe(send);
      expect(prototype.start).toBe(start);
      expect(
        isPatched(
          `@modelcontextprotocol/sdk/${path}` as Parameters<
            typeof isPatched
          >[0],
        ),
      ).toBe(false);
    }
  });
}
