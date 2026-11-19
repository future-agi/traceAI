import { context, trace } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor, type SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer, privacyFilter, registerSecret } from "@traceai/fi-core";
import { borrowConvertedSpanDestination } from "../src/converted-spans";

// These source-authored controls do not replace the unchanged real-library e2e
// matrix. No tests, package loading or RED verification ran in the writer lane.
const saved = { ...process.env };
beforeEach(() => { for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key]; });
afterEach(() => { process.env = { ...saved }; context.disable(); trace.disable(); });

test.each(["chat", "completions", "embeddings", "responses"])("D6 actual OpenAI producer installs native application context: %s", async route => {
  context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
  // Exercise the actual instrumentation patch, with in-memory SDK-shaped resource
  // classes so this control never opens a socket. Real SDK compatibility is separate.
  const { OpenAIInstrumentation } = require("@traceai/openai");
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  let active: any;
  class Resource {
    create() {
      active = trace.getActiveSpan();
      active.setAttribute("input.value", "application context sentinel");
      active.addEvent("application event", { note: "application event sentinel" });
      return Promise.resolve({ id: "response-id", object: route === "responses" ? "response" : "chat.completion", model: "test-model", choices: [{ index: 0, finish_reason: "stop", message: { role: "assistant", content: "answer" }, text: "answer" }], usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 }, data: [], output: [], status: "completed" });
    }
  }
  class Chat extends Resource {} class Completions extends Resource {} class Embeddings extends Resource {} class Responses extends Resource {}
  const module = { OpenAI: { Chat: { Completions: Chat }, Completions, Embeddings, Responses } };
  const instrumentor = new OpenAIInstrumentation({ traceConfig: { hideInputs: true } });
  try {
    instrumentor.setTracerProvider(FITracer.autoProvider(provider));
    instrumentor.manuallyInstrument(module);
    const Klass = route === "chat" ? Chat : route === "completions" ? Completions : route === "embeddings" ? Embeddings : Responses;
    await (new Klass().create as any)({ model: "test-model", messages: [{ role: "user", content: "private authored input" }], prompt: "private authored input", input: "private authored input" });
    const ended = exporter.getFinishedSpans()[0];
    expect(ended).toBeDefined();
    expect(active).toBe(ended);
    expect(ended.attributes["input.value"]).toBe("application context sentinel");
    expect(ended.events.find(event => event.name === "application event")?.attributes?.note).toBe("application event sentinel");
    // A later FI write/end must not replay its earlier hidden input over this
    // foreign overwrite. The detached FI policy still hides the content.
    const copy = privacyFilter({ hideInputs: true }).span(ended);
    expect(copy.attributes["input.value"]).toBe("__REDACTED__");
    expect(ended.attributes["input.value"]).toBe("application context sentinel");
    expect(ended.attributes).not.toHaveProperty("gen_ai.usage.input_tokens.audio");
    expect(ended.attributes).not.toHaveProperty("gen_ai.usage.output_tokens.reasoning");
  } finally { instrumentor.disable(); await provider.shutdown(); }
});

test("converted host-only forwarding preserves the original object and bytes", async () => {
  const exporter = new InMemorySpanExporter();
  const observer: SpanProcessor = { onStart() {}, onEnd() {}, async forceFlush() {}, async shutdown() {} };
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter), observer] });
  try {
    const destination = borrowConvertedSpanDestination(provider, observer)!;
    expect(destination).toBeDefined();
    const native: any = provider.getTracer("host").startSpan("native", { attributes: { note: "host-only-interim-secret" } });
    native.end();
    const ended = exporter.getFinishedSpans()[0]; const before = JSON.stringify(ended.attributes);
    registerSecret("host-only-interim-secret");
    exporter.reset();
    destination.processor.onEnd(ended);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0]).toBe(ended);
    expect(JSON.stringify(ended.attributes)).toBe(before);
    expect(ended.attributes.note).toBe("host-only-interim-secret");
    const fi = privacyFilter().span(ended);
    expect(fi.attributes.note).toBe("<redacted>");
    expect(ended.attributes.note).toBe("host-only-interim-secret");
    destination.revoke();
  } finally { await provider.shutdown(); }
});

test("FI view does not restore data lost to native count admission", async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanLimits: { attributeCountLimit: 1 }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const fi = new FITracer({ tracer: FITracer.autoProvider(provider).getTracer("auto") }).startSpan("native", { attributes: { first: "first ordinary value" } });
    fi.setAttribute("second", "second ordinary value"); fi.end();
    const native = exporter.getFinishedSpans()[0]; const before = JSON.stringify(native.attributes);
    const copy = privacyFilter().span(native);
    expect(native.attributes).toEqual({ first: "first ordinary value" });
    expect(native.droppedAttributesCount).toBe(1);
    expect(copy.attributes).not.toHaveProperty("second");
    expect(copy.attributes["traceai.capture.host_data_unknown"]).toBe(true);
    expect(copy.droppedAttributesCount).toBe(1);
    expect(JSON.stringify(native.attributes)).toBe(before);
  } finally { await provider.shutdown(); }
});
