// Copy this fixture beside an isolated package.json after installing the built
// tarballs and the desired @langchain/core version. No model or vendor calls.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { createServer } from "node:http";
import { once } from "node:events";

const require = createRequire(import.meta.url);
const mode = process.argv[2] ?? "automatic";
const hidden = process.argv[3] === "private";
assert.ok(["automatic", "manual-cjs", "manual-esm"].includes(mode));
// Exercise the public ESM export as well as CommonJS, without crossing the two
// separate LangChain module graphs when manually instrumenting them.
const load = mode === "manual-esm" ? (name) => import(name) : async (name) => require(name);
const { LangChainInstrumentation, isPatched } = await load("@traceai/langchain");
const { BasicTracerProvider, SimpleSpanProcessor } = await load("@opentelemetry/sdk-trace-base");
const { OTLPTraceExporter } = await load("@opentelemetry/exporter-trace-otlp-http");
const { SemanticConventions } = await load("@traceai/fi-semantic-conventions");

const requests = [];
const collector = createServer(async (request, response) => {
  const parts = [];
  for await (const part of request) parts.push(part);
  requests.push({ url: request.url, body: JSON.parse(Buffer.concat(parts).toString()) });
  response.writeHead(200, { "content-type": "application/json" });
  response.end("{}");
});
collector.listen(0, "127.0.0.1");
await once(collector, "listening");
const unfinished = new Set();
const provider = new BasicTracerProvider({
  spanProcessors: [
    {
      onStart(span) { unfinished.add(span.spanContext().spanId); },
      onEnd(span) { unfinished.delete(span.spanContext().spanId); },
      async forceFlush() {},
      async shutdown() {},
    },
    new SimpleSpanProcessor(new OTLPTraceExporter({
      url: `http://127.0.0.1:${collector.address().port}/v1/traces`,
    })),
  ],
});
const instrumentation = new LangChainInstrumentation({
  tracerProvider: provider,
  traceConfig: hidden ? { hideInputs: true, hideOutputs: true, hideErrorDetails: true } : {},
});

try {
  // Loading these AFTER initialization is essential to the automatic hook test.
  const manager = await load("@langchain/core/callbacks/manager");
  if (mode !== "automatic") {
    instrumentation.manuallyInstrument(manager);
    instrumentation.manuallyInstrument(manager); // Repeated setup must not duplicate spans.
  }
  assert.equal(isPatched(), true, `${mode} must patch core ${require("@langchain/core/package.json").version}`);
  const { RunnableLambda } = await load("@langchain/core/runnables");
  const { awaitAllCallbacks } = await load("@langchain/core/callbacks/promises");
  const secret = "synthetic-private-input";
  const makeChain = (name) => RunnableLambda.from((input) => `${input}-first`)
    .withConfig({ runName: `${name}_first` })
    .pipe(RunnableLambda.from((input) => `${input}-second`).withConfig({ runName: `${name}_second` }))
    .withConfig({ runName: `${name}_root` });

  assert.equal(await makeChain("invoke").invoke(secret), `${secret}-first-second`);
  {
    const chunks = [];
    for await (const chunk of await makeChain("stream").stream(secret)) chunks.push(chunk);
    assert.deepEqual(chunks, [`${secret}-first-second`]);
    const originalError = new Error(`failure for ${secret}`);
    const failing = RunnableLambda.from((input) => input)
      .withConfig({ runName: "error_first" })
      .pipe(RunnableLambda.from(() => { throw originalError; }).withConfig({ runName: "error_second" }))
      .withConfig({ runName: "error_root" });
    await assert.rejects(failing.invoke(secret), (error) => error === originalError);
    // This is the documented shutdown order, including the failure path.
    await awaitAllCallbacks();
    await provider.shutdown();
  }

  assert.equal(unfinished.size, 0, "every started span must end, including on transitional cores");
  assert.ok(requests.length > 0, "the real OTLP exporter must reach the local collector");
  assert.ok(requests.every((request) => request.url === "/v1/traces"));
  const spans = requests.flatMap(({ body }) => body.resourceSpans.flatMap(
    (resource) => resource.scopeSpans.flatMap((scope) => scope.spans),
  ));
  const names = spans.map((span) => span.name);
  {
    assert.equal(spans.length, 9, "three nested spans per invocation, stream, and error; no duplicates");
    assert.equal(new Set(spans.map((span) => span.spanId)).size, 9);
    for (const operation of ["invoke", "stream", "error"]) {
      const root = spans.find((span) => span.name === `${operation}_root`);
      assert.ok(root, `${operation} root must survive shutdown`);
      assert.ok(!root.parentSpanId, "a root must have no parent");
      for (const step of ["first", "second"]) {
        const child = spans.find((span) => span.name === `${operation}_${step}`);
        assert.ok(child);
        assert.equal(child.parentSpanId, root.spanId);
        assert.equal(child.traceId, root.traceId);
      }
    }
    for (const span of spans) {
      const failed = span.name === "error_root" || span.name === "error_second";
      assert.equal(span.status.code, failed ? 2 : 1);
      assert.ok(BigInt(span.endTimeUnixNano) >= BigInt(span.startTimeUnixNano));
      const attributes = Object.fromEntries(span.attributes.map(({ key, value }) => [key, value]));
      assert.equal(attributes[SemanticConventions.FI_SPAN_KIND]?.stringValue, "CHAIN");
      if (!hidden) assert.ok(attributes[SemanticConventions.INPUT_VALUE]);
    }
    if (hidden) assert.ok(!JSON.stringify(requests).includes(secret), "private content must not reach OTLP");
    else assert.ok(JSON.stringify(requests).includes(secret));
  }
  console.log(JSON.stringify({ core: require("@langchain/core/package.json").version, mode, hidden, spans: names, result: "PASS" }));
} finally {
  instrumentation.disable();
  await provider.shutdown();
  await new Promise((resolve, reject) => collector.close((error) => error ? reject(error) : resolve()));
}
