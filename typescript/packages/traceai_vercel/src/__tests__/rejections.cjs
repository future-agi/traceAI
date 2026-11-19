// Fresh-process regression for Node's actual unhandled-rejection semantics.
// Build first, then run one probe per process:
// node src/__tests__/rejections.cjs embed traced unhandled
// Arguments: embed|embedMany|generateObject baseline|traced unhandled|caught|late
// Unhandled probes MUST exit 1 with "provider failed"; other probes MUST exit 0.
const assert = require("node:assert/strict");
const { trace, context, SpanStatusCode } = require("@opentelemetry/api");
const { AsyncLocalStorageContextManager } = require("@opentelemetry/context-async-hooks");
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require("@opentelemetry/sdk-trace-base");

async function main() {
  const [method, mode, handling] = process.argv.slice(2);
  assert.ok(["embed", "embedMany", "generateObject"].includes(method));
  assert.ok(["baseline", "traced"].includes(mode));
  assert.ok(["unhandled", "caught", "late"].includes(handling));
  const original = await import("ai-v7");
  const { MockEmbeddingModelV4, MockLanguageModelV4 } = await import("ai-v7/test");
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider();
  const live = new Set();
  provider.addSpanProcessor(new SimpleSpanProcessor(exporter));
  provider.addSpanProcessor({
    onStart(span) { live.add(span.spanContext().spanId); },
    onEnd(span) { live.delete(span.spanContext().spanId); },
    async forceFlush() {}, async shutdown() {},
  });
  const manager = new AsyncLocalStorageContextManager().enable();
  context.setGlobalContextManager(manager);
  trace.setGlobalTracerProvider(provider);
  let sdk = original;
  let instrumentor;
  if (mode === "traced") {
    const { VercelAIInstrumentation } = require("../../dist/src/auto");
    instrumentor = new VercelAIInstrumentation();
    instrumentor.setTracerProvider(provider);
    sdk = instrumentor.manuallyInstrument(original, "7.0.158");
  }
  const failure = new Error("provider failed");
  let invocations = 0;
  const fail = async () => { invocations++; throw failure; };
  const model = method === "generateObject"
    ? new MockLanguageModelV4({ doGenerate: fail })
    : new MockEmbeddingModelV4({ doEmbed: fail });
  const schema = sdk.jsonSchema({ type: "object", properties: { hello: { type: "string" } }, required: ["hello"] });
  const promise = sdk[method]({ model, schema, value: "hi", values: ["hi"], prompt: "hi", maxRetries: 0 });
  if (handling === "unhandled") {
    // No process rejection listeners, .then(), .catch(), or await on this promise.
    // A swallowed SDK rejection would reach this timer and incorrectly exit 0.
    setTimeout(() => console.log("survived rejected SDK operation"), 200);
    return;
  }
  let unhandled = false;
  let handled = false;
  if (handling === "caught") {
    process.on("unhandledRejection", () => { throw new Error("caught operation became unhandled"); });
    await assert.rejects(promise, error => error === failure);
  } else {
    process.once("unhandledRejection", (error, rejected) => {
      assert.equal(error, failure);
      assert.equal(rejected, promise);
      unhandled = true;
      setImmediate(() => promise.catch(error => assert.equal(error, failure)));
    });
    process.once("rejectionHandled", rejected => {
      assert.equal(rejected, promise);
      handled = true;
    });
  }
  // Allow a turn for Node's rejection events, including delayed application catch.
  await new Promise(resolve => setTimeout(resolve, 200));
  if (handling === "late") {
    assert.ok(unhandled, "original promise must emit unhandledRejection");
    assert.ok(handled, "original promise must emit rejectionHandled");
  }
  assert.equal(invocations, 1);
  assert.equal(live.size, 0, "no orphan spans after a provider failure");
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, mode === "traced" ? 1 : 0);
  if (spans.length) assert.equal(spans[0].status.code, SpanStatusCode.ERROR);
  instrumentor?.disable();
  await provider.shutdown();
  context.disable(); trace.disable(); manager.disable();
  console.log(`PASS ${method} ${mode} ${handling}`);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
