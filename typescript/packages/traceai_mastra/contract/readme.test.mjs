// Run against an isolated install of the published package, not workspace mocks:
// node contract/readme.test.mjs /absolute/path/to/install [README.md]
import assert from "node:assert/strict";
import { once } from "node:events";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const install = resolve(process.argv[2]);
const readme = resolve(process.argv[3] ?? fileURLToPath(new URL("../README.md", import.meta.url)));
const requireInstalled = createRequire(join(install, "package.json"));
const ts = requireInstalled("typescript");
// Decode the actual OTLP/protobuf request with OpenTelemetry's generated schema.
const requireExporter = createRequire(requireInstalled.resolve("@opentelemetry/exporter-trace-otlp-proto"));
const transformer = dirname(requireExporter.resolve("@opentelemetry/otlp-transformer"));
const { opentelemetry } = requireInstalled(join(transformer, "generated/root.js"));
const ExportTraceServiceRequest = opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;

function attributes(values) {
  return Object.fromEntries(values.map(({ key, value }) => [key, value.stringValue]));
}

test("README setup compiles and exports native Mastra traces to the configured FI project", { timeout: 30_000 }, async (t) => {
  const source = await readFile(readme, "utf8");
  const snippets = [...source.matchAll(/```typescript\n([\s\S]*?)```/g)].map((match) => match[1]);
  assert.equal(snippets.length, 2, "Keep setup and awaited shutdown executable");
  const temp = await mkdtemp(join(install, "readme-contract-"));
  t.after(() => rm(temp, { recursive: true, force: true }));
  const tsFile = join(temp, "readme.mts");
  await writeFile(tsFile, snippets.join("\n"));
  const program = ts.createProgram([tsFile], {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.NodeNext,
    moduleResolution: ts.ModuleResolutionKind.NodeNext,
    strict: true,
    skipLibCheck: true,
    noEmit: true,
    types: ["node"],
    typeRoots: [join(install, "node_modules/@types")],
  });
  const diagnostics = ts.getPreEmitDiagnostics(program);
  assert.equal(diagnostics.length, 0, ts.formatDiagnosticsWithColorAndContext(diagnostics, {
    getCurrentDirectory: () => install,
    getCanonicalFileName: (name) => name,
    getNewLine: () => "\n",
  }));

  const requests = [];
  const server = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = Buffer.concat(chunks);
    requests.push({
      url: request.url,
      headers: request.headers,
      body: ExportTraceServiceRequest.decode(body),
    });
    response.writeHead(200, { "content-type": "application/x-protobuf" });
    response.end();
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => new Promise((done) => server.close(done)));
  const env = {
    FI_API_KEY: "synthetic-readme-api-key",
    FI_SECRET_KEY: "synthetic-readme-secret-key",
    FI_BASE_URL: `http://127.0.0.1:${server.address().port}`,
  };
  const previousEnv = Object.fromEntries(Object.keys(env).map((key) => [key, process.env[key]]));
  Object.assign(process.env, env);
  t.after(() => {
    for (const [key, value] of Object.entries(previousEnv)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });
  const jsFile = join(temp, "readme.mjs");
  const setup = ts.transpileModule(snippets[0], {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
  }).outputText;
  await writeFile(jsFile, setup);
  const { observability, mastra } = await import(pathToFileURL(jsFile).href);
  t.after(() => observability.shutdown());
  assert.ok(mastra);
  const instance = observability.getDefaultInstance();
  const exporters = instance.getExporters();
  assert.equal(exporters.length, 1, "Do not install a duplicate exporter");
  assert.equal(exporters[0].name, "future-agi");

  // Observe completion of the real exporter-bus promises, without sleeps or
  // replacing conversion/transport. Mastra span.end() itself returns void.
  const pending = [];
  const exporter = exporters[0];
  const exportEvent = exporter.exportTracingEvent.bind(exporter);
  exporter.exportTracingEvent = (event) => {
    const work = exportEvent(event);
    pending.push(work);
    return work;
  };
  // SpanType is a runtime enum. The values below are obtained from this install.
  const { SpanType } = requireInstalled("@mastra/core/observability");
  const root = instance.startSpan({
    name: "support-request",
    type: SpanType.AGENT_RUN,
    input: "Where is my order?",
  });
  const child = instance.startSpan({
    name: "lookup-order",
    type: SpanType.TOOL_CALL,
    parent: root,
    input: { orderId: "synthetic-123" },
  });
  child.end({ output: { status: "shipped" } });
  root.end({ output: "Your order has shipped." });
  const hidden = instance.startSpan({
    name: "private-run",
    type: SpanType.AGENT_RUN,
    input: "PRIVATE_INPUT_SENTINEL",
    tracingOptions: { hideInput: true, hideOutput: true },
  });
  hidden.end({ output: "PRIVATE_OUTPUT_SENTINEL" });
  const chunk = instance.startSpan({ name: "stream-chunk", type: SpanType.MODEL_CHUNK });
  chunk.end();
  assert.ok(pending.length > 0, "Native spans must reach the exporter bus");
  await Promise.all(pending);
  // Execute the README's shutdown instruction after exports have been queued.
  const shutdownFile = join(temp, "shutdown.mjs");
  await writeFile(shutdownFile, `import { observability } from './readme.mjs';\n${snippets[1]}`);
  await import(pathToFileURL(shutdownFile).href);

  assert.ok(requests.length > 0, "Shutdown must flush buffered spans");
  const spans = [];
  for (const request of requests) {
    assert.equal(request.url, "/tracer/v1/traces");
    assert.equal(request.headers["content-type"], "application/x-protobuf");
    assert.equal(request.headers["x-api-key"], env.FI_API_KEY);
    assert.equal(request.headers["x-secret-key"], env.FI_SECRET_KEY);
    for (const resource of request.body.resourceSpans) {
      const resourceAttrs = attributes(resource.resource.attributes);
      assert.equal(resourceAttrs["service.name"], "support-agent");
      assert.equal(resourceAttrs.project_name, "customer-support");
      assert.equal(resourceAttrs.project_type, "observe");
      for (const scope of resource.scopeSpans) spans.push(...scope.spans);
    }
  }
  // The declared 1.0.0 peer floor predates Mastra's excludeSpanTypes option.
  // The README pins a current package set for this option; retain legacy coverage
  // without pretending older Mastra versions suppress streaming chunks.
  const { version } = requireInstalled("@mastra/observability/package.json");
  assert.equal(spans.length, version === "1.0.0" ? 4 : 3, "Export ended spans once, applying supported chunk filtering");
  const byId = new Map(spans.map((span) => [Buffer.from(span.spanId).toString("hex"), span]));
  const parentWire = byId.get(root.id);
  const childWire = byId.get(child.id);
  assert.ok(parentWire);
  assert.ok(childWire);
  assert.deepEqual(childWire.traceId, parentWire.traceId);
  assert.deepEqual(childWire.parentSpanId, parentWire.spanId);
  assert.equal(attributes(parentWire.attributes)["gen_ai.span.kind"], "AGENT");
  assert.equal(attributes(childWire.attributes)["gen_ai.span.kind"], "TOOL");
  assert.equal(attributes(parentWire.attributes)["input.value"], "Where is my order?");
  assert.equal(attributes(parentWire.attributes)["output.value"], "Your order has shipped.");
  assert.deepEqual(JSON.parse(attributes(childWire.attributes)["output.value"]), { status: "shipped" });
  const payload = JSON.stringify(requests.map((request) => request.body));
  for (const secret of ["PRIVATE_INPUT_SENTINEL", "PRIVATE_OUTPUT_SENTINEL", env.FI_API_KEY, env.FI_SECRET_KEY]) {
    assert.ok(!payload.includes(secret), "Hidden I/O and auth values must not enter spans");
  }
});
