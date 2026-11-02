// Real packed-package contract. Dependencies are resolved only from the clean fixture.
// Run under a loopback-only network sandbox. No model/vendor calls or real keys.
const assert = require("node:assert/strict");
const { createRequire } = require("node:module");
const { pathToFileURL } = require("node:url");
const { createServer } = require("node:http");
const { once } = require("node:events");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const fixture = path.resolve(process.argv[2]);
const req = createRequire(path.join(fixture, "package.json"));
const { context, trace, propagation, ROOT_CONTEXT } = req("@opentelemetry/api");
const { NodeTracerProvider } = req("@opentelemetry/sdk-trace-node");
const { BatchSpanProcessor } = req("@opentelemetry/sdk-trace-base");
const { OTLPTraceExporter } = req("@opentelemetry/exporter-trace-otlp-http");
const { resourceFromAttributes } = req("@opentelemetry/resources");
const { Client } = req("@modelcontextprotocol/sdk/client/index.js");
const { StreamableHTTPClientTransport } = req(
  "@modelcontextprotocol/sdk/client/streamableHttp.js",
);
const { StdioClientTransport } = req(
  "@modelcontextprotocol/sdk/client/stdio.js",
);
const { ElicitRequestSchema, CreateMessageRequestSchema } = req(
  "@modelcontextprotocol/sdk/types.js",
);
const { z } = req("zod");
const version = JSON.parse(
  readFileSync(path.join(fixture, "node_modules/fastmcp/package.json")),
).version;
const current = version === "4.23.8" || version === "4.23.13";
const canary = "PRIVATE_CANARY_never_export";
const remote = "00-11111111111111111111111111111111-2222222222222222-01";
const meta = { traceparent: remote, custom: canary };
const tick = () => new Promise((resolve) => setImmediate(resolve));
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let provider;
let Fast, MCPInstrumentation;
async function load(esm = false) {
  Fast = esm
    ? await import(
        pathToFileURL(
          path.join(fixture, "node_modules/fastmcp/dist/FastMCP.js"),
        )
      )
    : req("fastmcp");
  ({ MCPInstrumentation } = esm
    ? await import(
        pathToFileURL(
          path.join(fixture, "node_modules/@traceai/mcp/dist/esm/index.js"),
        )
      )
    : req("@traceai/mcp"));
}
function setup(url) {
  provider = new NodeTracerProvider({
    resource: resourceFromAttributes({
      "service.name": "fastmcp-contract",
      project_name: "fastmcp-synthetic",
      project_type: "observe",
    }),
    spanProcessors: [
      new BatchSpanProcessor(
        new OTLPTraceExporter({
          url,
          headers: {
            FI_API_KEY: "synthetic-key",
            FI_SECRET_KEY: "synthetic-secret",
          },
        }),
        { scheduledDelayMillis: 60000 },
      ),
    ],
  });
  provider.register();
}
function instrumentation(options = {}) {
  const inst = new MCPInstrumentation({
    instrumentationConfig: { enabled: false },
  });
  assert.equal(inst.instrumentFastMCP(Fast, { version, ...options }), true);
  assert.equal(inst.instrumentFastMCP(Fast, { version, ...options }), true);
  return inst;
}
function server() {
  const app = new Fast.FastMCP({
    name: "offline-contract",
    version: "1.0.0",
    ping: { enabled: false },
    roots: { enabled: false },
    logger: { debug() {}, info() {}, log() {}, warn() {}, error() {} },
  });
  app.addTool({
    name: "add",
    parameters: z.object({
      a: z.number(),
      b: z.number(),
      delay: z.number().optional(),
      private: z.string().optional(),
    }),
    execute: async ({ a, b, delay: wait = 0 }, ctx) => {
      await delay(wait);
      const child = trace.getTracer("application").startSpan("nested");
      child.end();
      return String(a + b);
    },
  });
  app.addTool({
    name: "throws",
    execute: async () => {
      throw new Fast.UserError(canary);
    },
  });
  app.addTool({
    name: "returned-error",
    execute: async () => ({
      isError: true,
      content: [{ type: "text", text: canary }],
    }),
  });
  app.addTool({
    name: "content",
    parameters: z.object({ value: z.string(), secret: z.string().optional() }),
    execute: async () => ({
      content: [
        {
          type: "text",
          text: "allowed-output",
          ...(current ? { _meta: { private: canary } } : {}),
        },
        {
          type: "image",
          mimeType: "image/png",
          data: Buffer.from(canary).toString("base64"),
        },
      ],
      ...(current
        ? {
            structuredContent: { value: "allowed-structured", secret: canary },
            _meta: { private: canary },
          }
        : {}),
    }),
  });
  app.addTool({
    name: "stream",
    execute: async (_, ctx) => {
      await ctx.reportProgress({ progress: 1, total: 1 });
      await ctx.streamContent({ type: "text", text: canary });
      return "stream-done";
    },
  });
  app.addTool({
    name: "elicit",
    execute: async (_, ctx) => {
      const result = await ctx.elicit({
        message: canary,
        requestedSchema: {
          type: "object",
          properties: { ok: { type: "boolean" } },
        },
      });
      return result.action;
    },
  });
  app.addTool({
    name: "sampling",
    execute: async () => {
      const result = await app.sessions[0].server.createMessage({
        messages: [{ role: "user", content: { type: "text", text: canary } }],
        maxTokens: 1,
      });
      return result.content.text;
    },
  });
  app.addTool({
    name: "timeout",
    timeoutMs: 20,
    execute: async () => {
      await delay(70);
      return "late";
    },
  });
  app.addResource({
    uri: "test://private",
    name: "resource",
    load: async () => ({ text: canary }),
  });
  app.addResourceTemplate({
    uriTemplate: "test://template/{id}",
    name: "template",
    arguments: [{ name: "id", required: true }],
    load: async ({ id }) => ({ text: `${id}:${canary}` }),
  });
  app.addPrompt({
    name: "prompt",
    arguments: [{ name: "value", required: true }],
    load: async () => canary,
  });
  return app;
}
function client() {
  const client = new Client(
    { name: "contract-client", version: "1.0.0" },
    { capabilities: { elicitation: { form: {} }, sampling: {} } },
  );
  client.setRequestHandler(ElicitRequestSchema, async () => {
    // Application spans demonstrate propagation; the adapter does not invent LLM spans.
    trace.getTracer("application").startSpan("reverse-elicitation").end();
    return { action: "accept", content: { ok: true } };
  });
  client.setRequestHandler(CreateMessageRequestSchema, async () => {
    trace.getTracer("application").startSpan("reverse-sampling").end();
    return {
      role: "assistant",
      content: { type: "text", text: "synthetic-answer" },
      model: "synthetic",
    };
  });
  return client;
}
async function freePort() {
  const probe = createServer();
  probe.listen(0, "127.0.0.1");
  await once(probe, "listening");
  const port = probe.address().port;
  await new Promise((r) => probe.close(r));
  return port;
}
let bodies = [],
  paths = [],
  headers = [];
const spans = () =>
  bodies.flatMap((x) =>
    x.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)),
  );
const attrs = (span) =>
  Object.fromEntries(
    span.attributes.map((a) => [a.key, Object.values(a.value)[0]]),
  );
const ops = () => spans().filter((s) => s.name.startsWith("fastmcp "));
async function flush() {
  await tick();
  await provider.forceFlush();
}
async function tool(
  client,
  name = "add",
  args = { a: 2, b: 3 },
  customMeta = meta,
  options,
) {
  return client.callTool(
    { name, arguments: args, _meta: customMeta },
    undefined,
    options,
  );
}
async function httpCase({
  stateless,
  json,
  baseline = false,
  capture = false,
  esm = false,
}) {
  await load(esm);
  const inst = baseline
    ? undefined
    : instrumentation(
        capture
          ? {
              captureContent: { input: true, output: true, maxLength: 1024 },
              sessionId: "conversation-synthetic",
            }
          : {},
      );
  // Same instance can also cover generic transports without double wrapping/spans.
  if (inst)
    inst.manuallyInstrument({
      serverStreamableHTTPModule: req(
        "@modelcontextprotocol/sdk/server/streamableHttp.js",
      ),
    });
  // Loading both package distributions must not duplicate patches or operation spans.
  const Other = esm
    ? req("@traceai/mcp")
    : await import(
        pathToFileURL(
          path.join(fixture, "node_modules/@traceai/mcp/dist/esm/index.js"),
        )
      );
  const duplicate = new Other.MCPInstrumentation({
    instrumentationConfig: { enabled: false },
  });
  if (inst) assert.equal(duplicate.instrumentFastMCP(Fast, { version }), true);
  duplicate.disable();
  const app = server();
  const port = await freePort();
  const postTypes = [];
  const transport = new StreamableHTTPClientTransport(
    new URL(`http://127.0.0.1:${port}/mcp`),
    {
      requestInit: { headers: { authorization: `Bearer ${canary}` } },
      fetch: async (url, init) => {
        const result = await fetch(url, init);
        if (init?.method === "POST" && result.status === 200)
          postTypes.push(result.headers.get("content-type"));
        return result;
      },
    },
  );
  const c = client();
  inst?.instrumentTransport(transport);
  try {
    await app.start({
      transportType: "httpStream",
      httpStream: {
        host: "127.0.0.1",
        port,
        stateless,
        enableJsonResponse: json,
      },
    });
    await c.connect(transport);
    await flush();
    bodies = [];
    assert.equal((await tool(c)).content[0].text, "5");
    assert.ok(postTypes.length >= 2);
    assert.ok(
      postTypes.every((type) =>
        type?.includes(json ? "application/json" : "text/event-stream"),
      ),
    );
    await flush();
    if (baseline) {
      assert.equal(ops().length, 0);
      assert.equal(spans().filter((s) => s.name === "nested").length, 1);
      return;
    }
    assert.equal(ops().length, 1);
    const operation = ops()[0],
      child = spans().find((s) => s.name === "nested");
    assert.equal(operation.traceId, remote.split("-")[1]);
    assert.equal(operation.parentSpanId, remote.split("-")[2]);
    assert.equal(child.parentSpanId, operation.spanId);
    assert.equal(child.traceId, operation.traceId);
    assert.equal(attrs(operation)["fi.span.kind"], "TOOL");
    assert.equal(attrs(operation)["tool.name"], "add");
    assert.equal(operation.kind, 2);
    assert.equal(operation.status.code, 1);
    if (stateless) assert.equal(attrs(operation)["mcp.session.id"], undefined);
    else assert.equal(attrs(operation)["mcp.session.id"], transport.sessionId);
    if (capture)
      assert.equal(attrs(operation)["session.id"], "conversation-synthetic");
    // Overlapping requests in one session, deliberately reverse completion order.
    bodies = [];
    const parents = [
      remote,
      "00-33333333333333333333333333333333-4444444444444444-01",
    ];
    const results = await Promise.all(
      parents.map((traceparent, index) =>
        tool(
          c,
          "add",
          { a: index, b: 3, delay: index ? 1 : 40, private: canary },
          { traceparent, custom: canary },
        ),
      ),
    );
    assert.deepEqual(
      results.map((r) => r.content[0].text),
      ["3", "4"],
    );
    await flush();
    assert.equal(ops().length, 2);
    for (const parent of parents) {
      const span = ops().find((s) => s.traceId === parent.split("-")[1]);
      assert.ok(span);
      assert.equal(span.parentSpanId, parent.split("-")[2]);
      assert.equal(
        spans().find((s) => s.name === "nested" && s.traceId === span.traceId)
          .parentSpanId,
        span.spanId,
      );
    }
    // Independent sessions may reuse JSON-RPC IDs; they must not share operation state.
    const second = client();
    const secondTransport = new StreamableHTTPClientTransport(
      new URL(`http://127.0.0.1:${port}/mcp`),
    );
    try {
      await second.connect(secondTransport);
      await flush();
      bodies = [];
      await Promise.all([
        tool(c, "add", { a: 1, b: 2, delay: 30 }, { traceparent: parents[0] }),
        tool(second, "add", { a: 3, b: 4 }, { traceparent: parents[1] }),
      ]);
      await flush();
      assert.equal(ops().length, 2);
      assert.equal(new Set(ops().map((s) => s.traceId)).size, 2);
      if (!stateless)
        assert.equal(
          new Set(ops().map((s) => attrs(s)["mcp.session.id"])).size,
          2,
        );
    } finally {
      await second.close();
    }
    // Discovery, resource/template and prompt operations are not TOOL spans.
    bodies = [];
    await c.listTools();
    await c.listResources();
    await c.listResourceTemplates();
    await c.listPrompts();
    await c.ping();
    assert.match(
      (await c.readResource({ uri: "test://private" })).contents[0].text,
      /PRIVATE_CANARY/,
    );
    assert.match(
      (await c.readResource({ uri: "test://template/123" })).contents[0].text,
      /^123:/,
    );
    await c.getPrompt({ name: "prompt", arguments: { value: canary } });
    await flush();
    assert.equal(ops().length, 8);
    assert.ok(ops().every((s) => attrs(s)["fi.span.kind"] === "CHAIN"));
    assert.equal(
      ops().filter((s) => attrs(s)["mcp.resource.uri_hash"]).length,
      2,
    );
    if (!capture) assert.ok(!JSON.stringify(bodies).includes(canary));
    // Returned errors, thrown exceptions and SDK validation preserve behavior.
    bodies = [];
    assert.equal((await tool(c, "throws", {})).isError, true);
    assert.equal((await tool(c, "returned-error", {})).isError, true);
    await tool(c, "add", { a: "invalid", b: 3 }).catch(() => {});
    await tool(c, "not-registered", {}).catch(() => {});
    if (current) assert.equal((await tool(c, "timeout", {})).isError, true);
    await flush();
    assert.equal(ops().length, current ? 5 : 4);
    assert.ok(ops().every((s) => s.status.code === 2));
    assert.ok(!JSON.stringify(bodies).includes(canary));
    if (current && !json) {
      bodies = [];
      let progress = 0;
      assert.equal(
        (await tool(c, "stream", {}, meta, { onprogress: () => progress++ }))
          .content[0].text,
        "stream-done",
      );
      assert.equal(progress, 1);
      // Stateless elicitation requires response routing upstream doesn't offer on older versions.
      if (!stateless) {
        assert.equal((await tool(c, "elicit", {})).content[0].text, "accept");
        assert.equal(
          (await tool(c, "sampling", {})).content[0].text,
          "synthetic-answer",
        );
      }
      await flush();
      assert.equal(ops().length, stateless ? 1 : 3);
      if (!stateless)
        for (const [name, toolName] of [
          ["reverse-elicitation", "elicit"],
          ["reverse-sampling", "sampling"],
        ]) {
          assert.equal(
            spans().find((s) => s.name === name).parentSpanId,
            ops().find((s) => attrs(s)["tool.name"] === toolName).spanId,
          );
        }
      assert.ok(!JSON.stringify(bodies).includes(canary));
    }
    // Content capture is explicit and excludes meta/credentials/binary.
    if (capture) {
      bodies = [];
      const contentResult = await tool(c, "content", {
        value: "allowed-input",
        secret: canary,
      });
      assert.equal(
        contentResult.isError,
        undefined,
        JSON.stringify(contentResult),
      );
      await flush();
      const serialized = JSON.stringify(bodies);
      assert.ok(serialized.includes("allowed-input"));
      assert.ok(serialized.includes("allowed-output"));
      assert.ok(!serialized.includes(canary));
      process.env.FI_HIDE_INPUTS = "true";
      process.env.FI_HIDE_OUTPUT_TEXT = " TRUE ";
      bodies = [];
      await tool(c, "content", { value: "allowed-input" });
      await flush();
      assert.ok(!JSON.stringify(bodies).includes("allowed-"));
      delete process.env.FI_HIDE_INPUTS;
      delete process.env.FI_HIDE_OUTPUT_TEXT;
    }
    if (current) {
      bodies = [];
      const abort = new AbortController();
      const pending = tool(c, "add", { a: 2, b: 3, delay: 150 }, meta, {
        signal: abort.signal,
      });
      setTimeout(() => abort.abort(), 30);
      await assert.rejects(pending);
      await delay(170);
      await flush();
      assert.equal(ops().length, 1);
      // In FastMCP stateless mode a cancellation POST creates another server;
      // it cannot cancel the original operation. Preserve that server outcome.
      assert.equal(ops()[0].status.code, stateless ? 1 : 2);
    }
    bodies = [];
    inst.disable();
    await tool(c);
    await flush();
    assert.equal(ops().length, 0);
  } finally {
    await c.close();
    await app.stop();
    inst?.disable();
  }
  console.log(
    `PASS fastmcp ${version} ${esm ? "ESM" : "CJS"} HTTP stateless=${stateless} json=${json} capture=${capture}`,
  );
}
async function main() {
  if (process.argv[3] === "child") {
    await load();
    setup(process.argv[4]);
    instrumentation();
    const app = server();
    process.on("SIGTERM", async () => {
      await app.stop();
      await provider.shutdown();
      process.exit(0);
    });
    await app.start({ transportType: "stdio" });
    return;
  }
  const receiver = createServer((request, response) => {
    paths.push(request.url);
    headers.push(request.headers);
    let data = "";
    request.on("data", (x) => (data += x));
    request.on("end", () => {
      bodies.push(JSON.parse(data));
      response.setHeader("content-type", "application/json");
      response.end("{}");
    });
  });
  receiver.listen(0, "127.0.0.1");
  await once(receiver, "listening");
  const url = `http://127.0.0.1:${receiver.address().port}/v1/traces`;
  setup(url);
  try {
    await httpCase({ stateless: false, json: false, baseline: true });
    for (const stateless of [false, true])
      for (const json of [false, true]) await httpCase({ stateless, json });
    await httpCase({ stateless: false, json: true, capture: true, esm: true });
    bodies = [];
    const c = client();
    const transport = new StdioClientTransport({
      command: process.execPath,
      args: [__filename, fixture, "child", url],
      stderr: "pipe",
      env: { PATH: process.env.PATH || "" },
    });
    try {
      await c.connect(transport);
      assert.equal((await tool(c)).content[0].text, "5");
      process.kill(transport.pid, "SIGTERM");
      for (
        let i = 0;
        i < 100 && !ops().some((s) => attrs(s)["tool.name"] === "add");
        i++
      )
        await delay(20);
      const operation = ops().find((s) => attrs(s)["tool.name"] === "add");
      assert.ok(operation);
      assert.equal(operation.parentSpanId, remote.split("-")[2]);
      assert.equal(attrs(operation)["mcp.session.id"], undefined);
      assert.equal(
        spans().find((s) => s.name === "nested").parentSpanId,
        operation.spanId,
      );
    } finally {
      await c.close();
    }
    assert.ok(paths.every((p) => p === "/v1/traces"));
    assert.ok(
      headers.every(
        (h) =>
          h.fi_api_key === "synthetic-key" &&
          h.fi_secret_key === "synthetic-secret",
      ),
    );
    assert.ok(
      bodies.every((b) =>
        b.resourceSpans.every((r) =>
          r.resource.attributes.some(
            (a) =>
              a.key === "project_name" &&
              a.value.stringValue === "fastmcp-synthetic",
          ),
        ),
      ),
    );
    console.log(
      `PASS fastmcp ${version} real stdio process, OTLP wire/auth/project, shutdown, no logs/metrics`,
    );
  } finally {
    await provider.shutdown();
    receiver.closeAllConnections();
    await new Promise((r) => receiver.close(r));
    context.disable();
    trace.disable();
    propagation.disable();
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
