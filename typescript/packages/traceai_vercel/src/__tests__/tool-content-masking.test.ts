import { context } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { ReadableStream } from "node:stream/web";
import { VercelAIInstrumentation } from "../auto";
import { FISimpleSpanProcessor } from "../FISpanProcessor";

// Fully offline: the real AI SDK runs against its own mock model, spans go to an in-memory
// exporter, and every private value is a synthetic phrase. Tool content reaches the export in
// three places: gen_ai.tool.definitions.N.tool.json_schema (telemetry.ts, v7), the legacy alias
// tool.parameters (constants.ts mapping of ai.toolCall.args), and input.value/output.value.
const TOOL_DESC = "zebra lookup rationale for sealed oncology intake";
const TOOL_PROP = "sealed_oncology_field";
const TOOL_ARGS = "patient Zimmerman sealed arguments";
const TOOL_RESULT = "diagnosis sealed result for Zimmerman";
const PROMPT = "please summarise the sealed merger memo";
const PRIVATE = [TOOL_DESC, TOOL_PROP, TOOL_ARGS, TOOL_RESULT, PROMPT];

const load = new Function("name", "return import(name)") as (name: string) => Promise<any>;
const manager = new AsyncLocalStorageContextManager().enable();
beforeAll(() => { context.setGlobalContextManager(manager); });
afterAll(() => { context.disable(); manager.disable(); });

const saved = { ...process.env };
afterEach(() => { process.env = { ...saved }; });

type Route = "FITracer traceConfig" | "FISimpleSpanProcessor env flags";

describe.each([
  [5, "ai-v5", "5.0.283"], [6, "ai", "6.0.320"], [7, "ai-v7", "7.0.158"],
] as const)("AI SDK v%i (%s) tool content under hideInputs/hideOutputs", (major, name, version) => {
  const usage = () => major === 5 ? { inputTokens: 3, outputTokens: 4, totalTokens: 7 } : {
    inputTokens: { total: 3, noCache: 3, cacheRead: 0, cacheWrite: 0 }, outputTokens: { total: 4, text: 4, reasoning: 0 },
  };
  const finish = (reason: string) => major === 5 ? reason : { unified: reason, raw: reason };

  async function run(route: Route, hide: { hideInputs?: boolean; hideOutputs?: boolean }) {
    for (const key of Object.keys(process.env)) if (key.startsWith("FI_HIDE_")) delete process.env[key];
    if (route === "FISimpleSpanProcessor env flags") {
      if (hide.hideInputs) process.env.FI_HIDE_INPUTS = "true";
      if (hide.hideOutputs) process.env.FI_HIDE_OUTPUTS = "true";
    }
    const original = await load(name), mocks = await load(`${name}/test`);
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider();
    provider.addSpanProcessor(route === "FITracer traceConfig" ? new SimpleSpanProcessor(exporter) : new FISimpleSpanProcessor({ exporter }));
    const instrumentor = new VercelAIInstrumentation({ instrumentationConfig: { enabled: false },
      // The env route leaves traceConfig unset: fi-core reads FI_HIDE_* itself, and the package's own export filter runs on top.
      traceConfig: route === "FITracer traceConfig" ? hide : undefined });
    instrumentor.setTracerProvider(provider);
    instrumentor.enable();
    try {
      const sdk = instrumentor.manuallyInstrument(original, version);
      const Model = mocks[`MockLanguageModelV${major === 5 ? 2 : major === 6 ? 3 : 4}`];
      let call = 0;
      const model = new Model({ provider: "test.mock", modelId: "mock-model",
        doGenerate: async () => call++ === 0
          ? { content: [{ type: "tool-call", toolCallId: "t1", toolName: "lookup", input: JSON.stringify({ patient: TOOL_ARGS }) }], finishReason: finish("tool-calls"), usage: usage(), warnings: [] }
          : { content: [{ type: "text", text: "done" }], finishReason: finish("stop"), usage: usage(), warnings: [] },
        doStream: async () => ({ stream: new ReadableStream({ start(c) { c.enqueue({ type: "stream-start", warnings: [] }); c.close(); } }) }) });
      const stopWhen = sdk.stepCountIs ? sdk.stepCountIs(2) : sdk.isStepCount?.(2);
      await sdk.generateText({ model, prompt: PROMPT, maxRetries: 0, stopWhen,
        experimental_telemetry: { isEnabled: true, recordInputs: true, recordOutputs: true },
        tools: { lookup: sdk.tool({ description: TOOL_DESC,
          inputSchema: sdk.jsonSchema({ type: "object", properties: { patient: { type: "string", description: TOOL_PROP } }, required: ["patient"] }),
          execute: async () => ({ diagnosis: TOOL_RESULT }) }) } });
      await provider.forceFlush();
      return exporter.getFinishedSpans();
    } finally { instrumentor.disable(); await provider.shutdown(); }
  }
  const dump = (spans: readonly any[]) => JSON.stringify(spans.map(span => ({ name: span.name, attributes: span.attributes, events: span.events })));

  test.each(["FITracer traceConfig", "FISimpleSpanProcessor env flags"] as const)("control, nothing hidden (%s): the tool content really reaches the export", async route => {
    const spans = await run(route, {});
    expect(spans.length).toBeGreaterThan(0);
    const json = dump(spans);
    expect(json).toContain(TOOL_ARGS);
    expect(json).toContain(TOOL_RESULT);
    if (major === 7) expect(Object.keys(spans.find(s => s.attributes["gen_ai.span.kind"] === "LLM")!.attributes).some(k => /^gen_ai\.tool\.definitions\.\d+\.tool\.json_schema$/.test(k))).toBe(true);
  });

  test.each(["FITracer traceConfig", "FISimpleSpanProcessor env flags"] as const)("hideInputs and hideOutputs (%s): no tool schema, description, arguments, result or prompt in the exported span JSON", async route => {
    const spans = await run(route, { hideInputs: true, hideOutputs: true });
    expect(spans.length).toBeGreaterThan(0);
    for (const span of spans) {
      for (const [key, value] of Object.entries(span.attributes)) {
        if (/tool\.json_schema$|^tool\.parameters$|^tool\.description$|tool_call\.function\.arguments$/.test(key)) expect([undefined, "__REDACTED__"]).toContain(value);
      }
    }
    const json = dump(spans);
    for (const secret of PRIVATE) expect(json).not.toContain(secret);
  });

  test("hideInputs only (FITracer): tool definitions and the prompt are hidden, the tool result is not", async () => {
    const spans = await run("FITracer traceConfig", { hideInputs: true });
    const json = dump(spans);
    // The arguments also ride in the assistant output message (output side), so they are not asserted here.
    for (const secret of [TOOL_DESC, TOOL_PROP, PROMPT]) expect(json).not.toContain(secret);
    expect(json).toContain(TOOL_RESULT);
  });
});
