const assert = require("node:assert/strict");
const { createRequire } = require("node:module");
const { pathToFileURL } = require("node:url");
const path = require("node:path");
const root = process.argv[2];
const load = createRequire(path.join(root, "package.json"));
const { context, trace, propagation, ROOT_CONTEXT } =
  load("@opentelemetry/api");
const { AsyncLocalStorageContextManager } = load(
  "@opentelemetry/context-async-hooks",
);
const { W3CTraceContextPropagator } = load("@opentelemetry/core");
const manager = new AsyncLocalStorageContextManager().enable();
context.setGlobalContextManager(manager);
propagation.setGlobalPropagator(new W3CTraceContextPropagator());
const traceId = "11111111111111111111111111111111";
const traceparent = `00-${traceId}-2222222222222222-01`;
const request = () =>
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
        clientInfo: { name: "smoke", version: "1" },
        _meta: { traceparent },
      },
    }),
  });
async function checkTransport(module) {
  const transport = new module.WebStandardStreamableHTTPServerTransport({
    sessionIdGenerator: undefined,
    enableJsonResponse: true,
  });
  let observed;
  transport.onmessage = (message) => {
    observed = trace.getSpanContext(context.active())?.traceId;
    void transport.send({ jsonrpc: "2.0", id: message.id, result: {} });
  };
  await transport.start();
  try {
    const response = await context.with(ROOT_CONTEXT, () =>
      transport.handleRequest(request()),
    );
    assert.equal(response.status, 200);
    assert.equal(observed, traceId);
  } finally {
    await transport.close();
  }
}
(async () => {
  assert.throws(() => load.resolve("@modelcontextprotocol/sdk"), {
    code: "MODULE_NOT_FOUND",
  });
  const cjs = load("@traceai/mcp");
  const auto = new cjs.MCPInstrumentation();
  const split = load("@modelcontextprotocol/server");
  assert.equal(
    cjs.isPatched("@modelcontextprotocol/server"),
    true,
    "CJS top-level hook must run",
  );
  await checkTransport(split);
  auto.disable();
  assert.equal(cjs.isPatched("@modelcontextprotocol/server"), false);
  const esmPath = path.join(
    root,
    "node_modules/@traceai/mcp/dist/esm/index.js",
  );
  const esm = await import(pathToFileURL(esmPath).href);
  const serverEsm = await import(
    pathToFileURL(
      path.join(
        root,
        "node_modules/@modelcontextprotocol/server/dist/index.mjs",
      ),
    ).href
  );
  const manual = new esm.MCPInstrumentation({
    instrumentationConfig: { enabled: false },
  });
  manual.manuallyInstrument({ serverModule: serverEsm });
  await checkTransport(serverEsm);
  manual.disable();
  const { createMcpHandler } = load("mcp-handler");
  const handlerInstrumentation = new cjs.MCPInstrumentation({
    instrumentationConfig: { enabled: false },
  });
  let failedParent;
  const activeTraceId = () =>
    trace.getSpanContext(context.active())?.traceId ?? "none";
  const handler = createMcpHandler((server) => {
    handlerInstrumentation.instrumentServer(server);
    server.registerTool("echo", { inputSchema: {} }, async () => ({
      content: [{ type: "text", text: activeTraceId() }],
    }));
    server.registerTool("broken", { inputSchema: {} }, async () => {
      failedParent = activeTraceId();
      throw new Error("expected-handler-error");
    });
    server.registerResource("inventory", "inventory://sample", {}, async (uri) => ({
      contents: [{ uri: uri.href, text: activeTraceId() }],
    }));
    server.registerPrompt("inventory", {}, async () => ({
      messages: [{ role: "user", content: { type: "text", text: activeTraceId() } }],
    }));
  });
  const invoke = async (id, method, params, parent = traceparent) => {
    const result = await context.with(ROOT_CONTEXT, () =>
      handler(
        new Request("http://localhost/mcp", {
          method: "POST",
          headers: {
            "content-type": "application/json",
            accept: "application/json, text/event-stream",
          },
          body: JSON.stringify({
            jsonrpc: "2.0",
            id,
            method,
            params: { ...params, _meta: { traceparent: parent } },
          }),
        }),
      ),
    );
    assert.equal(result.status, 200);
    return result.text();
  };
  const secondTraceId = "33333333333333333333333333333333";
  const [toolResult, resourceResult, promptResult] = await Promise.all([
    invoke(2, "tools/call", { name: "echo", arguments: {} }),
    invoke(3, "resources/read", { uri: "inventory://sample" },
      `00-${secondTraceId}-4444444444444444-01`),
    invoke(4, "prompts/get", { name: "inventory" }),
  ]);
  assert.match(toolResult, new RegExp(traceId));
  assert.match(resourceResult, new RegExp(secondTraceId));
  assert.match(promptResult, new RegExp(traceId));
  const errorResult = await invoke(5, "tools/call", {
    name: "broken", arguments: {},
  });
  assert.match(errorResult, /expected-handler-error/);
  assert.equal(failedParent, traceId);
  handlerInstrumentation.disable();
  manager.disable();
  console.log(
    JSON.stringify({
      splitOnlyInstall: "PASS",
      cjsAutoHook: "PASS",
      esmManual: "PASS",
      mcpHandler230: "PASS",
    }),
  );
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
