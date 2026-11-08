import { context, trace } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { genkit } from "genkit";
import { mockModel } from "genkit/testing";
import { flushTracing } from "genkit/tracing";
import { enable } from "../index";
import { patchFetch, trackCoverage } from "../../../traceai_auto/src/wire/fetch";

const agentKey = Symbol.for("traceai.auto.agent.v1");
const globals = globalThis as unknown as Record<symbol, unknown>;
const savedAgent = globals[agentKey];
const appExporter = new InMemorySpanExporter();
const fiExporter = new InMemorySpanExporter();
const fiProcessor = new SimpleSpanProcessor(fiExporter);
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(appExporter), fiProcessor] });
const ai = genkit({});
const model = mockModel(ai, { name: "cold/mock", respond: { text: "cold answer", usage: { inputTokens: 2, outputTokens: 3, totalTokens: 5 } } });
const initialTerm = process.rawListeners("SIGTERM");

beforeAll(() => {
  fiProcessor.onStart = trackCoverage;
  expect(trace.setGlobalTracerProvider(provider)).toBe(true);
  globals[agentKey] = { processor: fiProcessor, config: { apiKey: "test-api-key", secretKey: "test-secret-key" } };
});
afterAll(async () => {
  await flushTracing();
  await provider.shutdown();
  trace.disable();
  context.disable();
  for (const listener of process.rawListeners("SIGTERM")) {
    if (!initialTerm.includes(listener)) process.removeListener("SIGTERM", listener as (...args: unknown[]) => void);
  }
  if (savedAgent === undefined) delete globals[agentKey];
  else globals[agentKey] = savedAgent;
});

it("enables before the first Genkit call on an existing OTel 2.x provider without initializing or replacing telemetry", async () => {
  const telemetryGlobals = globalThis as unknown as { __GENKIT_TELEMETRY_INSTRUMENTED?: unknown };
  const instrumentation = telemetryGlobals.__GENKIT_TELEMETRY_INSTRUMENTED;
  const hooks = [process.rawListeners("SIGTERM"), process.rawListeners("SIGINT")];
  expect(enable()).toEqual({ status: "instrumented" });
  expect(telemetryGlobals.__GENKIT_TELEMETRY_INSTRUMENTED).toBe(instrumentation);
  expect([process.rawListeners("SIGTERM"), process.rawListeners("SIGINT")]).toEqual(hooks);
  const response = await ai.generate({ model, prompt: "cold prompt" });
  expect(response.text).toBe("cold answer");
  await provider.forceFlush();
  const native = appExporter.getFinishedSpans();
  const mapped = fiExporter.getFinishedSpans();
  expect(mapped).toHaveLength(native.length);
  expect(mapped).toHaveLength(2); // generate util + model action, no extra agent spans.
  expect(mapped.find(span => span.name === "cold/mock")?.attributes).toMatchObject({
    "gen_ai.span.kind": "LLM", "gen_ai.request.model": "mock", "gen_ai.usage.total_tokens": 5,
  });
  expect(native.find(span => span.name === "cold/mock")?.attributes["gen_ai.span.kind"]).toBeUndefined();
  expect((trace.getTracerProvider() as unknown as { getDelegate(): unknown }).getDelegate()).toBe(provider);

  const applicationSpan = provider.getTracer("app").startSpan("kept application span");
  applicationSpan.end();
  await provider.forceFlush();
  expect(fiExporter.getFinishedSpans().at(-1)).toBe(applicationSpan);
  expect(appExporter.getFinishedSpans().at(-1)).toBe(applicationSpan);
});

it("suppresses the real wire hook during Genkit model calls and intermediate native SDK spans only", async () => {
  const savedFetch = globalThis.fetch;
  globalThis.fetch = jest.fn(async () => new Response(JSON.stringify({
    id: "local-response", model: "mock", choices: [{ message: { role: "assistant", content: "local answer" }, finish_reason: "stop" }],
    usage: { prompt_tokens: 2, completion_tokens: 3, total_tokens: 5 },
  }), { headers: { "content-type": "application/json" } }));
  const captured: unknown[] = [];
  const hook = patchFetch(4 * 1024 * 1024, result => { captured.push(result); }, []);
  const request = async () => {
    const response = await fetch("https://api.openai.com/v1/chat/completions", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ model: "mock", messages: [{ role: "user", content: "hello" }] }),
    });
    await response.json();
  };
  try {
    const coveredModel = mockModel(ai, { name: "cold/dedup", respond: async () => {
      await request();
      const native = provider.getTracer("native-sdk").startSpan("native intermediary");
      try { await context.with(trace.setSpan(context.active(), native), request); }
      finally { native.end(); }
      return "dedup answer";
    } });
    expect((await ai.generate({ model: coveredModel, prompt: "hello" })).text).toBe("dedup answer");
    await Promise.all([...hook.pending]);
    expect(captured).toHaveLength(0);
    // Coverage must end with the model context: a later uninstrumented HTTP call
    // must still be captured, proving this isn't blanket wire suppression.
    await request();
    await Promise.all([...hook.pending]);
    expect(captured).toHaveLength(1);
  } finally {
    hook.remove();
    globalThis.fetch = savedFetch;
  }
});
