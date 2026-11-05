// Real LangChain classes, packed CJS/ESM exports, and a real OTel exporter.
// No provider credentials or external requests. Run beside the installed tarballs.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const mode = process.argv[2] ?? "manual-cjs";
const privacy = process.argv[3] ?? "public";
const load = mode === "manual-esm" ? (name) => import(name) : async (name) => require(name);
if (privacy === "environment") {
  process.env.FI_HIDE_INPUTS = "true";
  process.env.FI_HIDE_OUTPUTS = "true";
  process.env.FI_HIDE_ERROR_DETAILS = "true";
}
const { LangChainInstrumentation } = await load("@traceai/langchain");
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = await load("@opentelemetry/sdk-trace-base");
const exporter = new InMemorySpanExporter();
const unfinished = new Set();
const provider = new BasicTracerProvider({ spanProcessors: [
  { onStart(s) { unfinished.add(s.spanContext().spanId); }, onEnd(s) { unfinished.delete(s.spanContext().spanId); }, async forceFlush() {}, async shutdown() {} },
  new SimpleSpanProcessor(exporter),
] });
const instrumentation = new LangChainInstrumentation({
  tracerProvider: provider,
  traceConfig: privacy === "options" ? { hideOutputs: true } : undefined,
});
const manager = await load("@langchain/core/callbacks/manager");
const models = await load("@langchain/core/language_models/chat_models");
if (mode !== "automatic") {
  instrumentation.manuallyInstrument(manager, models);
  instrumentation.manuallyInstrument(manager, models);
}
const { awaitAllCallbacks } = await load("@langchain/core/callbacks/promises");
const { BaseRetriever } = await load("@langchain/core/retrievers");
const { Document } = await load("@langchain/core/documents");
const { AIMessage, AIMessageChunk } = await load("@langchain/core/messages");
const { ChatGenerationChunk } = await load("@langchain/core/outputs");
const documentSecret = "synthetic-private-document";
const metadataSecret = "synthetic-private-metadata";
const answer = "synthetic-private-answer";
class Retriever extends BaseRetriever {
  lc_namespace = ["synthetic"];
  async _getRelevantDocuments() {
    return [0, 1].map(() => new Document({ pageContent: documentSecret, metadata: { source: metadataSecret } }));
  }
}
class Model extends models.BaseChatModel {
  constructor(zero = false) { super({}); this.zero = zero; }
  _llmType() { return "synthetic"; }
  async _generate() {
    const message = new AIMessage(answer);
    // Early cores predate the constructor field, but custom adapters can expose it.
    message.usage_metadata = { input_tokens: 100, output_tokens: 10, total_tokens: 110,
      input_token_details: { cache_read: this.zero ? 0 : 20, cache_creation: this.zero ? 0 : 30 } };
    return { generations: [{ text: answer, message }] };
  }
  async *_streamResponseChunks(_messages, options) {
    try {
      for (let i = 0; i < 3; i++) {
        if (options.signal?.aborted) throw options.signal.reason;
        yield new ChatGenerationChunk({ text: answer, message: new AIMessageChunk(answer) });
      }
    } finally { this.closed = (this.closed ?? 0) + 1; }
  }
}
const checks = [];
async function check(name, fn) {
  try { await fn(); checks.push({ name, pass: true }); }
  catch (error) { checks.push({ name, pass: false, error: error.message }); }
}
async function spansNamed(name) {
  await awaitAllCallbacks(); await provider.forceFlush();
  return exporter.getFinishedSpans().filter((s) => s.name === name);
}
try {
  await check("retrieval document content and metadata follow output privacy", async () => {
    await new Retriever().invoke("query", { runName: "retrieval" });
    const [span] = await spansNamed("retrieval");
    assert.ok(span);
    const wire = JSON.stringify(span.attributes);
    for (const secret of [documentSecret, metadataSecret]) {
      assert.equal(wire.includes(secret), privacy === "public");
    }
    for (const i of [0, 1]) for (const field of ["content", "metadata"]) {
      const value = span.attributes[`retrieval.documents.${i}.document.${field}`];
      assert.equal(privacy === "public" ? typeof value : value, privacy === "public" ? "string" : "__REDACTED__");
    }
  });
  for (const zero of [false, true]) await check(`cache usage is normalized including zeros: ${zero}`, async () => {
    const name = `cache-${zero}`;
    await new Model(zero).invoke("query", { runName: name });
    const [span] = await spansNamed(name);
    for (const [key, value] of Object.entries({ input_tokens: 100, output_tokens: 10, total_tokens: 110, cache_read_tokens: zero ? 0 : 20, cache_write_tokens: zero ? 0 : 30 })) {
      assert.equal(span.attributes[`gen_ai.usage.${key}`], value, key);
    }
    if (privacy !== "public") assert.ok(!JSON.stringify(span.attributes).includes(answer));
  });
  for (const action of ["break", "return", "throw", "abort", "complete"]) await check(`stream ${action} ends once and preserves iterator semantics`, async () => {
    const model = new Model();
    const name = `stream-${action}`;
    const abort = new AbortController();
    const options = { runName: name, signal: abort.signal, runId: "12345678-1234-4234-8234-" + String(checks.length).padStart(12, "0") };
    const runId = options.runId;
    const stream = await model.stream("query", options);
    if (action === "break") { for await (const _chunk of stream) break; }
    else if (action === "complete") { let count = 0; for await (const _chunk of stream) count++; assert.equal(count, 3); }
    else {
      assert.equal((await stream.next()).done, false);
      if (action === "return") await stream.return();
      if (action === "throw") {
        const error = new Error("synthetic-consumer-error");
        await assert.rejects(stream.throw(error), (received) => received === error);
      }
      if (action === "abort") { abort.abort(new Error("synthetic-abort")); await assert.rejects(stream.next()); }
    }
    assert.equal(options.runId, runId, "do not mutate caller config");
    assert.equal(model.closed, 1, "delegate return to the upstream iterator");
    const spans = await spansNamed(name);
    assert.equal(spans.length, 1);
    assert.equal(spans[0].status.code === 1, action === "complete", "partial output is not successful completion");
    assert.ok(!unfinished.has(spans[0].spanContext().spanId));
  });
  await check("cancelling one stream does not end a concurrent stream", async () => {
    const first = await new Model().stream("query", { runName: "first" });
    const second = await new Model().stream("query", { runName: "second" });
    await Promise.all([first.next(), second.next()]);
    await first.return();
    assert.equal((await spansNamed("first")).length, 1);
    assert.equal((await spansNamed("second")).length, 0);
    for await (const _chunk of second) { /* consume the remaining response */ }
    assert.equal((await spansNamed("second"))[0].status.code, 1);
  });
  await check("disable and re-enable restore model and callback patches", async () => {
    instrumentation.disable();
    await new Model().invoke("query", { runName: "disabled" });
    assert.equal((await spansNamed("disabled")).length, 0);
    instrumentation.enable();
    for await (const _chunk of await new Model().stream("query", { runName: "re-enabled" })) break;
    const [span] = await spansNamed("re-enabled");
    assert.ok(span);
    assert.notEqual(span.status.code, 1);
  });
  await check("all started spans ended", async () => {
    await awaitAllCallbacks(); assert.equal(unfinished.size, 0);
    const spans = exporter.getFinishedSpans();
    assert.equal(new Set(spans.map((s) => s.spanContext().spanId)).size, spans.length);
  });
  console.log(JSON.stringify({ core: require("@langchain/core/package.json").version, mode, privacy, checks }, null, 2));
  process.exitCode = checks.some((check) => !check.pass) ? 1 : 0;
} finally { instrumentation.disable(); await provider.shutdown(); }
