// Real Genkit and its built-in mock model: no provider network, no mocked Genkit modules.
import { context, trace } from "@opentelemetry/api";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { genkit, z, type GenerateRequest, type TelemetryConfig } from "genkit";
import { enableTelemetry, flushTracing } from "genkit/tracing";
import { mockModel } from "genkit/testing";
import { enable, type GenkitAutoSpanProcessor, type GenkitReadableSpan } from "../index";

const AGENT_KEY = Symbol.for("traceai.auto.agent.v1");
const globals = globalThis as unknown as Record<symbol, unknown>;
const nativeSpans: GenkitReadableSpan[] = [];
const exporter = new InMemorySpanExporter();
const target = new SimpleSpanProcessor(exporter);
const application = {
  onStart() {},
  onEnd(span: GenkitReadableSpan) { nativeSpans.push(span); },
  async forceFlush() {},
  async shutdown() {},
};
const ai = genkit({});
const failure = new Error("real model failure");
const failing = mockModel(ai, { name: "auto/failure", respond: () => { throw failure; } });
const model = mockModel(ai, {
  name: "auto/mock",
  info: { supports: { tools: true, multiturn: true } },
  respond: (request: GenerateRequest, { sendChunk }: { sendChunk(chunk: string): void }) => {
    if (request.messages.at(-1)?.content.some(part => part.toolResponse)) {
      sendChunk("final ");
      return { text: "final answer", usage: { inputTokens: 21, outputTokens: 4, totalTokens: 25 } };
    }
    return { toolRequests: [{ name: "lookup", input: { id: 7 } }], usage: { inputTokens: 11, outputTokens: 3, totalTokens: 14 } };
  },
});
const lookup = ai.defineTool({ name: "lookup", description: "lookup", inputSchema: z.object({ id: z.number() }), outputSchema: z.string() },
  async ({ id }) => `record-${id}`);
const flow = ai.defineFlow({ name: "autoFlow", inputSchema: z.string(), outputSchema: z.string(), streamSchema: z.string() }, async (query, { sendChunk }) => {
  const prompt = await ai.run("prepare", async () => query.trim());
  const { stream, response } = ai.generateStream({ model, prompt, tools: [lookup] });
  for await (const chunk of stream) sendChunk(chunk.text);
  return (await response).text;
});

async function streamed() {
  const result = flow.stream("stream prompt");
  const chunks: string[] = [];
  for await (const chunk of result.stream) chunks.push(chunk);
  return { chunks, output: await result.output };
}

let baseline: Awaited<ReturnType<typeof streamed>>;
let baselineNames: string[];
let provider: unknown;
let baselineGenerate: typeof ai.generate;
let handlers: Function[][];
const initialTerm = process.rawListeners("SIGTERM");
const savedAgent = globals[AGENT_KEY];

beforeAll(async () => {
  // Genkit owns the global OTel 1.x provider and the Dev UI export branch. The
  // agent's dedicated 2.x export processor coexists in the SAME live provider.
  await enableTelemetry({ spanProcessors: [application, target] as unknown as TelemetryConfig["spanProcessors"] });
  baseline = await streamed();
  await expect(ai.generate({ model: failing, prompt: "fail" })).rejects.toBe(failure);
  await flushTracing();
  baselineNames = nativeSpans.map(span => span.name).sort();
  provider = trace.getTracerProvider();
  baselineGenerate = ai.generate;
  handlers = [process.rawListeners("SIGTERM"), process.rawListeners("SIGINT"), process.rawListeners("beforeExit")];
  globals[AGENT_KEY] = { processor: target, config: { apiKey: "test-api-key", secretKey: "test-secret-key", traceConfig: {} } };
  expect(enable()).toEqual({ status: "instrumented" });
});
beforeEach(() => { exporter.reset(); nativeSpans.length = 0; });
afterAll(async () => {
  await target.shutdown();
  const delegate = (trace.getTracerProvider() as unknown as { getDelegate(): { shutdown(): Promise<void> } }).getDelegate();
  await delegate.shutdown();
  trace.disable();
  context.disable();
  for (const listener of process.rawListeners("SIGTERM")) if (!initialTerm.includes(listener)) process.removeListener("SIGTERM", listener as (...args: unknown[]) => void);
  if (savedAgent === undefined) delete globals[AGENT_KEY];
  else globals[AGENT_KEY] = savedAgent;
});

describe("zero-code Genkit 1.42.0 integration", () => {
  it("matches no-agent streaming output and errors, with one mapped export per native span", async () => {
    expect(await streamed()).toEqual(baseline);
    await expect(ai.generate({ model: failing, prompt: "fail" })).rejects.toBe(failure);
    await flushTracing();
    const mapped = exporter.getFinishedSpans();
    expect(nativeSpans.map(span => span.name).sort()).toEqual(baselineNames);
    expect(mapped).toHaveLength(nativeSpans.length);
    expect(new Set(mapped.map(span => span.spanContext().spanId)).size).toBe(nativeSpans.length);
    expect(mapped.filter(span => span.attributes["gen_ai.span.kind"] === "LLM")).toHaveLength(3);
    const failed = mapped.find(span => span.name === "auto/failure")!;
    expect(failed.status.code).toBe(2);
    expect(failed.events.some(event => event.name === "exception")).toBe(true);
    expect(ai.generate).toBe(baselineGenerate);
  });

  it("maps tool-loop kinds, parenting and tokens without double-counting generate/flow usage", async () => {
    expect(await flow("  prompt  ")).toBe("final answer");
    await flushTracing();
    const spans = exporter.getFinishedSpans();
    const models = spans.filter(span => span.attributes["gen_ai.span.kind"] === "LLM");
    expect(models).toHaveLength(2);
    expect(models.map(span => span.attributes["gen_ai.request.model"])).toEqual(["mock", "mock"]);
    expect(spans.find(span => span.name === "lookup")?.attributes["gen_ai.span.kind"]).toBe("TOOL");
    expect(spans.find(span => span.name === "autoFlow")?.attributes["gen_ai.span.kind"]).toBe("CHAIN");
    expect(spans.reduce((total, span) => total + Number(span.attributes["gen_ai.usage.total_tokens"] ?? 0), 0)).toBe(39);
    const ids = new Set(spans.map(span => span.spanContext().spanId));
    for (const modelSpan of models) expect(ids.has(modelSpan.parentSpanContext!.spanId)).toBe(true);
  });

  it("leaves app/Dev UI native spans, provider, telemetry configuration and signal hooks intact", async () => {
    await flow("native input marker");
    await flushTracing();
    expect(trace.getTracerProvider()).toBe(provider);
    expect([process.rawListeners("SIGTERM"), process.rawListeners("SIGINT"), process.rawListeners("beforeExit")]).toEqual(handlers);
    const mapped = exporter.getFinishedSpans();
    for (const native of nativeSpans) {
      expect(native.attributes["gen_ai.span.kind"]).toBeUndefined();
      expect(native.attributes["input.value"]).toBeUndefined();
      const copy = mapped.find(span => span.spanContext().spanId === native.spanContext().spanId)!;
      expect(copy).not.toBe(native);
      expect(copy.startTime).toEqual(native.startTime);
      expect(copy.endTime).toEqual(native.endTime);
      expect(copy.duration).toEqual(native.duration);
      expect(copy.resource).toBe((native as unknown as { resource: unknown }).resource);
      expect(copy.attributes["gen_ai.span.kind"]).toBeDefined();
    }
    expect(nativeSpans.find(span => span.name === "autoFlow")!.attributes["genkit:input"]).toContain("native input marker");
  });

  it("honors hide flags on a separate FI branch while app telemetry keeps its original content", async () => {
    const privateExporter = new InMemorySpanExporter();
    const branch = new SimpleSpanProcessor(privateExporter);
    expect(enable({ spanProcessor: branch as unknown as GenkitAutoSpanProcessor, traceConfig: { hideInputs: true, hideOutputs: true } }).status).toBe("instrumented");
    const delegate = (trace.getTracerProvider() as unknown as { getDelegate(): { addSpanProcessor(processor: unknown): void } }).getDelegate();
    delegate.addSpanProcessor(branch);
    await flow("private-content-marker");
    await flushTracing();
    await branch.forceFlush();
    const hidden = privateExporter.getFinishedSpans();
    expect(hidden.length).toBeGreaterThan(0);
    expect(JSON.stringify(hidden.map(span => span.attributes))).not.toContain("private-content-marker");
    expect(hidden.find(span => span.name === "autoFlow")!.attributes["input.value"]).toBe("__REDACTED__");
    expect(nativeSpans.find(span => span.name === "autoFlow")!.attributes["genkit:input"]).toContain("private-content-marker");
    expect(hidden.reduce((total, span) => total + Number(span.attributes["gen_ai.usage.total_tokens"] ?? 0), 0)).toBe(39);
    await branch.shutdown();
  });

  it("retains the real response class and helpers for non-streamed generation", async () => {
    const response = await ai.generate({ model, prompt: "non-streamed", tools: [lookup] });
    expect(response.text).toBe("final answer");
    expect(typeof response.toJSON).toBe("function");
    expect(typeof response.assertValid).toBe("function");
    expect(response.toJSON().message?.content).toEqual([{ text: "final answer" }]);
    response.assertValid();
    await flushTracing();
    expect(exporter.getFinishedSpans().filter(span => span.attributes["gen_ai.span.kind"] === "LLM")).toHaveLength(2);
  });
});
