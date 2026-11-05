// Packed-package regression contract with real ChatOpenAI and localhost SSE.
// Synthetic key only; run once without instrumentation as an upstream control.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { createServer } from "node:http";
import { once } from "node:events";
const require = createRequire(import.meta.url);
const mode = process.argv[2] ?? "manual-cjs";
const hidden = process.argv[3] === "private";
const enabled = mode !== "disabled";
const load = mode === "manual-esm" ? (name) => import(name) : async (name) => require(name);
const { LangChainInstrumentation } = await load("@traceai/langchain");
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = await load("@opentelemetry/sdk-trace-base");
const exporter = new InMemorySpanExporter();
const starts = new Map();
const ends = new Map();
const provider = new BasicTracerProvider({ spanProcessors: [
  { onStart(s) { starts.set(s.spanContext().spanId, s); }, onEnd(s) { const id = s.spanContext().spanId; ends.set(id, (ends.get(id) ?? 0) + 1); }, async forceFlush() {}, async shutdown() {} },
  new SimpleSpanProcessor(exporter),
] });
const instrumentation = new LangChainInstrumentation({ tracerProvider: provider, instrumentationConfig: { enabled },
  traceConfig: { hideInputs: hidden, hideOutputs: hidden, hideErrorDetails: hidden } });
const manager = await load("@langchain/core/callbacks/manager");
const models = await load("@langchain/core/language_models/chat_models");
const runnables = await load("@langchain/core/runnables");
if (enabled && mode !== "automatic") instrumentation.manuallyInstrument(manager, models, runnables);
const { ChatOpenAI } = await load("@langchain/openai");
const { awaitAllCallbacks } = await load("@langchain/core/callbacks/promises");
const privateAnswer = "synthetic-private-answer";
const server = createServer(async (req, res) => {
  const body = [];
  for await (const part of req) body.push(part);
  const { model } = JSON.parse(Buffer.concat(body).toString());
  const timers = [];
  res.on("close", () => timers.forEach(clearTimeout));
  res.writeHead(200, { "content-type": "text/event-stream" });
  res.flushHeaders();
  const chunk = (content, finish_reason = null) => res.write(`data: ${JSON.stringify({ id: "synthetic", object: "chat.completion.chunk", created: 1, model, choices: [{ index: 0, delta: { content }, finish_reason }] })}\n\n`);
  timers.push(setTimeout(() => {
    chunk(privateAnswer);
    if (model === "complete") timers.push(setTimeout(() => { chunk("-last", "stop"); res.end("data: [DONE]\n\n"); }, 180));
  }, 120));
});
server.listen(0, "127.0.0.1");
await once(server, "listening");
const baseURL = `http://127.0.0.1:${server.address().port}/v1`;
const model = (name = "stall") => new ChatOpenAI({ apiKey: "synthetic-key", model: name, maxRetries: 0, configuration: { baseURL } });
const checks = [];
async function check(name, fn) {
  try { await fn(); checks.push({ name, pass: true }); }
  catch (e) { checks.push({ name, pass: false, error: e.stack }); }
}
async function flush() { await awaitAllCallbacks(); await provider.forceFlush(); }
const named = (name) => exporter.getFinishedSpans().filter((s) => s.name === name);
// A watchdog is a test failure, not a simulated request deadline. Close the
// server connection before cleanup so a broken wrapper cannot hang the suite.
async function bounded(promise, ms = 2000) {
  let timer;
  try { return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => { server.closeAllConnections(); reject(new Error("test watchdog")); }, ms); })]); }
  finally { clearTimeout(timer); }
}
function checkTree(prefix, complete = false) {
  const root = named(`${prefix}-root`)[0];
  const inner = named(`${prefix}-inner`)[0];
  assert.ok(root, "outer chain ended"); assert.ok(inner, "inner chain ended");
  const spans = exporter.getFinishedSpans().filter((s) => s.spanContext().traceId === root.spanContext().traceId);
  // Some adapters ignore a bound runName in transform(). Locate the real LLM
  // by its parent and kind rather than depending on that upstream name behavior.
  const parent = (s) => s.parentSpanContext?.spanId ?? s.parentSpanId;
  const leaf = spans.find((s) => parent(s) === inner.spanContext().spanId && s.attributes["fi.span.kind"] === "LLM");
  assert.ok(leaf, "model ended");
  assert.equal(typeof leaf.attributes["gen_ai.response.time_to_first_token"], "number", "timing survives cancellation too");
  assert.equal(parent(root), undefined);
  assert.equal(parent(inner), root.spanContext().spanId);
  assert.equal(parent(leaf), inner.spanContext().spanId);
  for (const s of spans) {
    assert.equal(ends.get(s.spanContext().spanId), 1);
    assert.equal(s.spanContext().traceId, root.spanContext().traceId);
  }
  for (const s of [root, inner, leaf]) {
    assert.equal(s.status.code, complete ? 1 : 2, `${s.name} status`);
    if (!complete) {
      assert.equal(s.attributes["langchain.stream.cancelled"], true);
      assert.equal(s.attributes["gen_ai.usage.total_tokens"], undefined);
      assert.ok(!JSON.stringify(s.attributes).includes(privateAnswer), "partial output is not success");
    }
  }
}
function chain(prefix, complete = false) {
  const inner = runnables.RunnableLambda.from((x) => x).withConfig({ runName: `${prefix}-inner-input` })
    .pipe(model(complete ? "complete" : "stall").withConfig({ runName: `${prefix}-llm` }))
    .withConfig({ runName: `${prefix}-inner` });
  return runnables.RunnableLambda.from((x) => x).withConfig({ runName: `${prefix}-input` })
    .pipe(inner).withConfig({ runName: `${prefix}-root` });
}
try {
  await check("request timeout survives a stalled SSE response", async () => {
    const start = performance.now();
    let error;
    try { await bounded((async () => { for await (const _chunk of await model().stream("query", { timeout: 350 })) {} })(), 1200); }
    catch (e) { error = e; }
    assert.ok(error, "upstream deadline rejects");
    assert.ok(performance.now() - start < 1100, "deadline was not replaced by watchdog");
    await flush();
  });
  await check("caller signal retains upstream precedence over timeout", async () => {
    const controller = new AbortController();
    const complete = (async () => {
      for await (const _chunk of await model("complete").stream("query", { timeout: 80, signal: controller.signal })) {}
    })();
    if (require("@langchain/core/package.json").version === "0.2.0") await bounded(complete);
    else await assert.rejects(bounded(complete));
    assert.equal(controller.signal.aborted, false, "never abort the caller's controller");
    await flush();
  });
  if (enabled) {
    await check("chain options preserve a model request's timeout", async () => {
      await assert.rejects(bounded((async () => {
        for await (const _chunk of await chain("chain-timeout").stream("query", { timeout: 350 })) {}
      })(), 1200), (error) => error.message !== "test watchdog");
      await flush();
      assert.equal(named("chain-timeout-root")[0]?.status.code, 2);
    });
    await check("first-token latency is numeric seconds and recorded once", async () => {
      for await (const _chunk of await model("complete").stream("query", { runName: "timing" })) {}
      await flush();
      const [span] = named("timing");
      const seconds = span.attributes["gen_ai.response.time_to_first_token"];
      assert.equal(typeof seconds, "number");
      assert.ok(seconds >= 0.10 && seconds < 0.28, `first chunk, not total duration: ${seconds}`);
      if (hidden) assert.ok(!JSON.stringify(span.attributes).includes(privateAnswer));
    });
    for (const action of ["break", "return", "throw", "abort", "pending-return", "pending-abort", "complete"]) await check(`nested Runnable stream ${action} ends every span once`, async () => {
      const controller = new AbortController();
      const stream = await chain(action, action === "complete").stream("query", { signal: controller.signal });
      await bounded((async () => {
        if (action === "break") { for await (const _chunk of stream) break; }
        else if (action === "complete") { for await (const _chunk of stream) {} }
        else {
          assert.equal((await stream.next()).done, false);
          if (action === "return") await stream.return();
          if (action === "throw") {
            const error = new Error("synthetic-consumer-error");
            await assert.rejects(stream.throw(error), (e) => e === error);
          }
          if (action === "abort") { controller.abort(); await assert.rejects(stream.next()); }
          if (action.startsWith("pending-")) {
            const next = stream.next();
            // Attach a rejection handler before cancellation to exercise the
            // actual in-flight read without creating an unhandled rejection.
            const settled = Promise.allSettled([next]);
            if (action === "pending-return") await stream.return();
            else controller.abort();
            await settled;
          }
        }
        await stream.return();
      })());
      await flush();
      checkTree(action, action === "complete");
    });
    await check("closing a nested stream leaves concurrent chains open", async () => {
      const first = await chain("first").stream("query");
      const second = await chain("second", true).stream("query");
      await Promise.all([first.next(), second.next()]);
      await bounded(first.return()); await flush();
      checkTree("first");
      assert.equal(named("second-root").length, 0);
      for await (const _chunk of second) {} await flush(); checkTree("second", true);
    });
    await check("pure Runnable transforms close without any model leaf", async () => {
      const pure = runnables.RunnableLambda.from((x) => x).pipe(runnables.RunnableLambda.from(async function* () {
        yield "first"; yield "second";
      }).withConfig({ runName: "pure-transform" })).withConfig({ runName: "pure-root" });
      for await (const _chunk of await pure.stream("query")) break;
      await flush();
      for (const name of ["pure-root", "pure-transform"]) {
        assert.equal(named(name).length, 1); assert.equal(named(name)[0].status.code, 2);
      }
    });
    await check("disable and re-enable restore Runnable patches", async () => {
      instrumentation.disable();
      for await (const _chunk of await chain("disabled-chain", true).stream("query")) {}
      await flush(); assert.equal(named("disabled-chain-root").length, 0);
      instrumentation.enable();
      for await (const _chunk of await chain("reenabled").stream("query")) break;
      await flush(); checkTree("reenabled");
    });
    await check("all started spans end exactly once", async () => {
      await flush();
      for (const [id, span] of starts) assert.equal(ends.get(id), 1, span.name);
    });
  }
} finally {
  instrumentation.disable(); await provider.shutdown(); server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
}
console.log(JSON.stringify({ core: require("@langchain/core/package.json").version, mode, hidden, checks,
  spans: [...starts].map(([id, s]) => ({ name: s.name, id, parent: s.parentSpanContext?.spanId ?? s.parentSpanId, ends: ends.get(id) ?? 0 })),
}, null, 2));
process.exitCode = checks.some((c) => !c.pass) ? 1 : 0;
