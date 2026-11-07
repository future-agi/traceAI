import { context, trace, SpanStatusCode } from "@opentelemetry/api";
import { suppressTracing } from "@opentelemetry/core";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { ReadableStream } from "node:stream/web";
import { VercelAIInstrumentation, enable, enableVercelInstrumentation } from "../auto";
import { REDACTED_VALUE, setSession } from "@traceai/fi-core";
import { SESSION_ID } from "@traceai/fi-semantic-conventions";
import { privacyFilter } from "../../../traceai_auto/src/export-privacy";

const load = new Function("name", "return import(name)") as (name: string) => Promise<any>;
const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider();
const liveSpans = new Map<string, string>();
provider.addSpanProcessor(new SimpleSpanProcessor(exporter));
provider.addSpanProcessor({
  onStart(span) { liveSpans.set(span.spanContext().spanId, span.name); },
  onEnd(span) { liveSpans.delete(span.spanContext().spanId); },
  async forceFlush() {},
  async shutdown() {},
});
const manager = new AsyncLocalStorageContextManager().enable();
const spans = (kind?: string) => exporter.getFinishedSpans().filter(span => !kind || span.attributes["gen_ai.span.kind"] === kind);
const baseline = <T>(fn: () => T): T => context.with(suppressTracing(context.active()), fn);

beforeAll(() => { context.setGlobalContextManager(manager); trace.setGlobalTracerProvider(provider); });
afterEach(() => expect([...liveSpans.values()]).toEqual([]));
afterAll(async () => { await provider.shutdown(); context.disable(); trace.disable(); manager.disable(); });

describe.each([
  [5, "ai-v5", "5.0.283"], [6, "ai", "6.0.320"], [7, "ai-v7", "7.0.158"],
] as const)("real AI SDK v%i (%s)", (major, name, version) => {
  let original: any;
  let sdk: any;
  let mocks: any;
  let instrumentor: VercelAIInstrumentation;
  const usage = () => major === 5 ? { inputTokens: 3, outputTokens: 4, totalTokens: 7 } : {
    inputTokens: { total: 3, noCache: 3, cacheRead: 0, cacheWrite: 0 },
    outputTokens: { total: 4, text: 4, reasoning: 0 },
  };
  const finishReason = () => major === 5 ? "stop" : { unified: "stop", raw: "stop" };
  function model(text = "hello", overrides: any = {}) {
    const Model = mocks[`MockLanguageModelV${major === 5 ? 2 : major === 6 ? 3 : 4}`];
    return new Model({ provider: "test.mock", modelId: "mock-model", doGenerate: {
      content: [{ type: "text", text }], finishReason: finishReason(), usage: usage(), warnings: [],
    }, doStream: () => Promise.resolve({ stream: new ReadableStream({ start(controller) {
      controller.enqueue({ type: "stream-start", warnings: [] });
      controller.enqueue({ type: "text-start", id: "text" });
      controller.enqueue({ type: "text-delta", id: "text", delta: text });
      controller.enqueue({ type: "text-end", id: "text" });
      controller.enqueue({ type: "finish", finishReason: finishReason(), usage: usage() });
      controller.close();
    } }) }), ...overrides });
  }
  function embedding() {
    const Model = mocks[`MockEmbeddingModelV${major === 5 ? 2 : major === 6 ? 3 : 4}`];
    return new Model({ provider: "test.mock", modelId: "embed-model", maxEmbeddingsPerCall: 1,
      doEmbed: async ({ values }: any) => ({ embeddings: values.map(() => [0.123456, 0.654321]), usage: { tokens: values.length * 2 }, warnings: [] }),
    });
  }
  beforeAll(async () => {
    original = await load(name);
    mocks = await load(`${name}/test`);
    instrumentor = new VercelAIInstrumentation({ instrumentationConfig: { enabled: false } });
    instrumentor.setTracerProvider(provider);
    instrumentor.enable();
    sdk = instrumentor.manuallyInstrument(original, version);
  });
  beforeEach(() => exporter.reset());
  afterAll(() => instrumentor.disable());

  test("generateText needs no telemetry option and preserves baseline result/helpers", async () => {
    const options = { model: model(), prompt: "hi", maxRetries: 0 };
    const plain = await baseline(() => original.generateText({ ...options, model: model() }));
    const result = await sdk.generateText(options);
    expect(result.text).toBe(plain.text);
    expect(result.constructor).toBe(plain.constructor);
    expect(result.response.messages).toEqual(plain.response.messages);
    expect(spans("LLM")).toHaveLength(1);
    expect(spans("LLM")[0].attributes["gen_ai.request.model"]).toBe("mock-model");
    expect(spans("LLM")[0].attributes["gen_ai.usage.input_tokens"]).toBe(3);
    expect((options as any).experimental_telemetry).toBeUndefined();
    expect((options as any).telemetry).toBeUndefined();
  });

  test("streamText preserves synchronous result, streams and response helpers", async () => {
    const plain = baseline(() => original.streamText({ model: model(), prompt: "hi" }));
    const result = sdk.streamText({ model: model(), prompt: "hi" });
    expect(result).not.toBeInstanceOf(Promise);
    expect(result.constructor).toBe(plain.constructor);
    expect(typeof result.toTextStreamResponse).toBe("function");
    const read = async (stream: any) => { let text = ""; for await (const part of stream) text += part; return text; };
    expect(await read(result.textStream)).toBe(await read(plain.textStream));
    expect(await result.text).toBe("hello");
    expect(spans("LLM")).toHaveLength(1);
    expect(spans("LLM")[0].attributes["output.value"]).toBe("hello");
  });

  test.each(["streamText", "streamObject"])("unread %s results are not consumed or read ahead by instrumentation", async method => {
    function pending() {
      let pulls = 0;
      let controller: any;
      const source = new ReadableStream({ start(value) { controller = value; }, pull() { pulls++; } });
      return { model: model("", { doStream: async () => ({ stream: source }) }), pulls: () => pulls,
        finish() {
          controller.enqueue({ type: "stream-start", warnings: [] });
          controller.enqueue({ type: "text-start", id: "pending" });
          controller.enqueue({ type: "text-delta", id: "pending", delta: method === "streamObject" ? '{"hello":"world"}' : "done" });
          controller.enqueue({ type: "text-end", id: "pending" });
          controller.enqueue({ type: "finish", finishReason: finishReason(), usage: usage() });
          controller.close();
        },
      };
    }
    const plainSource = pending();
    const tracedSource = pending();
    const schema = sdk.jsonSchema({ type: "object", properties: { hello: { type: "string" } }, required: ["hello"] });
    const plain = baseline(() => original[method]({ model: plainSource.model, prompt: "hi", schema }));
    const traced = sdk[method]({ model: tracedSource.model, prompt: "hi", schema });
    await new Promise(resolve => setImmediate(resolve));
    await new Promise(resolve => setImmediate(resolve));
    expect(tracedSource.pulls()).toBe(plainSource.pulls());
    expect(spans("LLM")).toHaveLength(0);
    plainSource.finish(); tracedSource.finish();
    const read = async (value: any) => { let text = ""; for await (const part of value.textStream) text += part; return text; };
    expect(await read(traced)).toBe(await read(plain));
    expect(spans("LLM")).toHaveLength(1);
  });

  test.each([
    ["streamText", false], ["streamText", true], ["streamObject", false], ["streamObject", true],
  ] as const)("%s raw transport failure (partial=%s) retains baseline output/callbacks and ends every span", async (method, partial) => {
    const failure = new Error("stream transport failed");
    const run = async (api: any) => {
      const onError = jest.fn();
      const chunks = partial ? [
        { type: "stream-start", warnings: [] }, { type: "text-start", id: "partial" },
        { type: "text-delta", id: "partial", delta: method === "streamObject" ? '{"hello":"partial' : "partial" },
      ] : [];
      const broken = model("", { doStream: async () => ({ stream: new ReadableStream({ pull(controller) {
        if (chunks.length) controller.enqueue(chunks.shift());
        else controller.error(failure);
      } }, { highWaterMark: 0 }) }) });
      const schema = api.jsonSchema({ type: "object", properties: { hello: { type: "string" } }, required: ["hello"] });
      const result = api[method]({ model: broken, prompt: "hi", schema, maxRetries: 0, onError });
      const parts: any[] = [];
      let readError: unknown;
      try { for await (const part of result.fullStream) parts.push(part.type === "error" ? { type: part.type, error: part.error } : part.type); }
      catch (error) { readError = error; }
      return { parts, readError, errors: onError.mock.calls.map(([event]) => event.error) };
    };
    const expected = await baseline(() => run(original));
    expect(await run(sdk)).toEqual(expected);
    expect(expected.readError).toBe(failure);
    expect(spans("LLM")).toHaveLength(1);
    expect(spans("LLM")[0].status.code).toBe(SpanStatusCode.ERROR);
    expect([...liveSpans.values()]).toEqual([]);
  });

  test("generateObject and streamObject preserve structured output and schemas", async () => {
    const schema = sdk.jsonSchema({ type: "object", properties: { hello: { type: "string" } }, required: ["hello"], additionalProperties: false });
    const options = { prompt: "json", schema, maxRetries: 0 };
    const plain = await baseline(() => original.generateObject({ ...options, model: model('{"hello":"world"}') }));
    const generated = await sdk.generateObject({ ...options, model: model('{"hello":"world"}') });
    expect(generated.object).toEqual(plain.object);
    const streamed = sdk.streamObject({ ...options, model: model('{"hello":"world"}') });
    expect(streamed).not.toBeInstanceOf(Promise);
    for await (const _part of streamed.partialObjectStream) { /* consume as an application does */ }
    expect(await streamed.object).toEqual(plain.object);
    expect(spans("LLM")).toHaveLength(2);
  });

  test("embed and chunked embedMany retain vectors only in application results", async () => {
    const plain = await baseline(() => original.embed({ model: embedding(), value: "hello" }));
    const single = await sdk.embed({ model: embedding(), value: "hello" });
    const many = await sdk.embedMany({ model: embedding(), values: ["a", "b"], maxParallelCalls: 2 });
    expect(single.embedding).toEqual(plain.embedding);
    expect(many.embeddings).toEqual([plain.embedding, plain.embedding]);
    expect(spans("EMBEDDING").length).toBeGreaterThanOrEqual(2);
    expect(JSON.stringify(spans().map(span => span.attributes))).not.toContain("0.123456");
  });

  test("tool execution chains callbacks, preserves output, and binds TOOL context", async () => {
    const callback = jest.fn();
    let activeKind: unknown;
    const toolModel = model("", { doGenerate: {
      content: [{ type: "tool-call", toolCallId: "tool-1", toolName: "weather", input: '{"city":"Paris"}' }],
      finishReason: major === 5 ? "tool-calls" : { unified: "tool-calls", raw: "tool-calls" }, usage: usage(), warnings: [],
    } });
    const result = await sdk.generateText({ model: toolModel, prompt: "weather?", maxRetries: 0,
      tools: { weather: sdk.tool({ inputSchema: sdk.jsonSchema({ type: "object", properties: { city: { type: "string" } }, required: ["city"] }),
        execute: async ({ city }: any) => {
          activeKind = trace.getActiveSpan()?.spanContext().spanId;
          return { city, temperature: 22 };
        },
      }) }, onStepFinish: callback,
    });
    expect(result.toolResults[0].output).toEqual({ city: "Paris", temperature: 22 });
    expect(callback).toHaveBeenCalledTimes(1);
    expect(spans("TOOL")).toHaveLength(1);
    const messages = String(spans("LLM")[0].attributes["gen_ai.output.messages"]);
    expect(messages).toContain("tool-1");
    expect(messages).toContain("Paris");
    expect(activeKind).toBe(spans("TOOL")[0].spanContext().spanId);
    expect(spans("TOOL")[0].attributes["input.value"]).toBe('{"city":"Paris"}');
    expect(spans("TOOL")[0].attributes["input.mime_type"]).toBe("application/json");
    expect(spans("TOOL")[0].attributes["tool.parameters"]).toBeUndefined();
  });

  test.each(["FI environment flags", "authoritative export boundary", "both"] as const)(
    "tool arguments cannot bypass input/output hiding through legacy aliases (%s)", async boundary => {
      const sourceMasking = boundary !== "authoritative export boundary";
      const exportMasking = boundary !== "FI environment flags";
      const savedInputs = process.env.FI_HIDE_INPUTS;
      const savedOutputs = process.env.FI_HIDE_OUTPUTS;
      instrumentor.disable();
      process.env.FI_HIDE_INPUTS = String(sourceMasking);
      process.env.FI_HIDE_OUTPUTS = String(sourceMasking);
      const masked = new VercelAIInstrumentation();
      masked.setTracerProvider(provider);
      try {
        const api = masked.manuallyInstrument(original, version);
        const callback = jest.fn();
        const output = { result: "patient-private-output" };
        const execute = jest.fn(async (_input: any) => output);
        const toolModel = model("", { doGenerate: {
          content: [{ type: "tool-call", toolCallId: "private-tool", toolName: "lookup", input: '{"patient":"patient-private-input"}' }],
          finishReason: major === 5 ? "tool-calls" : { unified: "tool-calls", raw: "tool-calls" }, usage: usage(), warnings: [],
        } });
        const result = await api.generateText({ model: toolModel, prompt: "lookup", maxRetries: 0,
          tools: { lookup: api.tool({ inputSchema: api.jsonSchema({ type: "object", properties: { patient: { type: "string" } }, required: ["patient"] }), execute }) },
          onStepFinish: callback,
        });
        expect(result.toolResults[0].output).toEqual(output);
        expect(execute).toHaveBeenCalledTimes(1);
        expect(execute.mock.calls[0][0]).toEqual({ patient: "patient-private-input" });
        expect(callback).toHaveBeenCalledTimes(1);
        expect(spans("TOOL")).toHaveLength(1);
        const tool = spans("TOOL")[0];
        const privacy = privacyFilter({ hideInputs: true, hideOutputs: true });
        // The agent accepts OTel 1.x spans at runtime as well as its own 2.x spans.
        const exported = exportMasking ? spans().map(span => privacy.span(span as unknown as Parameters<typeof privacy.span>[0])) : spans();
        const exportedTool = exported.find(span => span.spanContext().spanId === tool.spanContext().spanId)!;
        expect(exportedTool.attributes["input.value"]).toBe(REDACTED_VALUE);
        expect(exportedTool.attributes["output.value"]).toBe(REDACTED_VALUE);
        expect(exportedTool.attributes["tool.parameters"]).toBeUndefined();
        const attributes = JSON.stringify(exported.map(span => span.attributes));
        expect(attributes).not.toContain("patient-private-input");
        expect(attributes).not.toContain("patient-private-output");
        if (!sourceMasking) {
          // Export scrubbing uses a snapshot, not a mutation of the SDK span.
          expect(tool.attributes["input.value"]).toBe('{"patient":"patient-private-input"}');
          expect(tool.attributes["output.value"]).toBe(JSON.stringify(output));
        }
      } finally {
        masked.disable();
        if (savedInputs === undefined) delete process.env.FI_HIDE_INPUTS;
        else process.env.FI_HIDE_INPUTS = savedInputs;
        if (savedOutputs === undefined) delete process.env.FI_HIDE_OUTPUTS;
        else process.env.FI_HIDE_OUTPUTS = savedOutputs;
        instrumentor.enable();
      }
    },
  );

  test("provider errors and callbacks retain the original exception without retries", async () => {
    const failure = new Error("provider failed");
    const call = jest.fn(async () => { throw failure; });
    const options = { model: model("", { doGenerate: call }), prompt: "hi", maxRetries: 0 };
    await expect(sdk.generateText(options)).rejects.toBe(failure);
    expect(call).toHaveBeenCalledTimes(1);
    expect(spans("LLM")[0].status.code).toBe(SpanStatusCode.ERROR);
    const callbackFailure = new Error("application callback failed");
    const callback = jest.fn(() => { throw callbackFailure; });
    const outcome = (fn: () => Promise<any>) => fn().then(result => result.text, error => error);
    const expected = await baseline(() => outcome(() => original.generateText({ model: model(), prompt: "hi", onStepFinish: callback })));
    const actual = await outcome(() => sdk.generateText({ model: model(), prompt: "hi", onStepFinish: callback }));
    expect(actual).toBe(expected);
    expect(callback).toHaveBeenCalledTimes(2);
  });

  test.each(["embed", "embedMany", "generateObject"])("%s provider rejection preserves the error and ends its model span", async method => {
    const failure = new Error("provider failed");
    const call = jest.fn(async () => { throw failure; });
    const callModel = method === "generateObject" ? model("", { doGenerate: call }) : embedding();
    if (method !== "generateObject") callModel.doEmbed = call;
    const schema = sdk.jsonSchema({ type: "object", properties: { hello: { type: "string" } }, required: ["hello"] });
    const options = { model: callModel, prompt: "hi", value: "hi", values: ["hi"], schema, maxRetries: 0 };
    await expect(sdk[method](options)).rejects.toBe(failure);
    expect(call).toHaveBeenCalledTimes(1);
    const modelSpans = spans(method === "generateObject" ? "LLM" : "EMBEDDING");
    expect(modelSpans).toHaveLength(1);
    expect(modelSpans[0].status.code).toBe(SpanStatusCode.ERROR);
    expect([...liveSpans.values()]).toEqual([]);
  });

  test("suppression, explicit opt-out and input/output recording flags are respected", async () => {
    await context.with(suppressTracing(context.active()), () => sdk.generateText({ model: model(), prompt: "secret" }));
    const telemetryKey = major === 7 ? "telemetry" : "experimental_telemetry";
    await sdk.generateText({ model: model(), prompt: "secret", [telemetryKey]: { isEnabled: false } });
    expect(spans()).toHaveLength(0);
    await sdk.generateText({ model: model(), prompt: "secret", [telemetryKey]: { isEnabled: true, recordInputs: false, recordOutputs: false } });
    const attrs = JSON.stringify(spans().map(span => span.attributes));
    expect(attrs).not.toContain("secret");
    expect(attrs).not.toContain("hello");
  });

  test("provider context is a model/embedding span for nested wire deduplication", async () => {
    const kinds: unknown[] = [];
    const callModel = model();
    const generate = callModel.doGenerate;
    callModel.doGenerate = (...args: any[]) => {
      const span = trace.getActiveSpan();
      // Check the export after end, rather than depend on facade implementation details.
      kinds.push(span?.spanContext().spanId);
      return generate.apply(callModel, args);
    };
    await sdk.generateText({ model: callModel, prompt: "hi" });
    expect(kinds[0]).toBe(spans("LLM")[0].spanContext().spanId);
    if (major === 7) {
      const embedModel = embedding();
      const embed = embedModel.doEmbed;
      embedModel.doEmbed = (...args: any[]) => { kinds.push(trace.getActiveSpan()?.spanContext().spanId); return embed.apply(embedModel, args); };
      await sdk.embed({ model: embedModel, value: "hi" });
      expect(kinds[1]).toBe(spans("EMBEDDING")[0].spanContext().spanId);
    }
  });

  test("gateway string models preserve provider resolution and model context", async () => {
    const globals = globalThis as any;
    const saved = globals.AI_SDK_DEFAULT_PROVIDER;
    const active: string[] = [];
    const textModel = model();
    const objectModel = model('{"hello":"world"}');
    const embedModel = embedding();
    for (const item of [textModel, objectModel, embedModel]) {
      const key = item === embedModel ? "doEmbed" : "doGenerate";
      const originalMethod = item[key];
      item[key] = (...args: any[]) => { active.push(trace.getActiveSpan()!.spanContext().spanId); return originalMethod.apply(item, args); };
    }
    const languageModel = jest.fn((id: string) => id === "gateway/object" ? objectModel : textModel);
    const embeddingModel = jest.fn(() => embedModel);
    globals.AI_SDK_DEFAULT_PROVIDER = { specificationVersion: major === 5 ? "v2" : major === 6 ? "v3" : "v4", languageModel, embeddingModel, textEmbeddingModel: embeddingModel };
    try {
      expect((await sdk.generateText({ model: "gateway/text", prompt: "hi" })).text).toBe("hello");
      expect((await sdk.embed({ model: "gateway/embed", value: "hi" })).embedding).toEqual([0.123456, 0.654321]);
      const schema = sdk.jsonSchema({ type: "object", properties: { hello: { type: "string" } }, required: ["hello"] });
      expect((await sdk.generateObject({ model: "gateway/object", prompt: "json", schema })).object).toEqual({ hello: "world" });
      expect(languageModel).toHaveBeenCalledTimes(2);
      expect(embeddingModel).toHaveBeenCalledTimes(1);
      const modelIds = [...spans("LLM"), ...spans("EMBEDDING")].map(span => span.spanContext().spanId);
      active.forEach(id => expect(modelIds).toContain(id));
      expect(new Set(active).size).toBe(3);
    } finally { globals.AI_SDK_DEFAULT_PROVIDER = saved; }
  });

  test("FI trace configuration hides native and dispatcher content without affecting results", async () => {
    instrumentor.disable();
    const masked = new VercelAIInstrumentation({ traceConfig: { hideInputs: true, hideOutputs: true } });
    masked.setTracerProvider(provider);
    try {
      const api = masked.manuallyInstrument(original, version);
      const result = await api.generateText({ model: model("private-output"), prompt: "private-input" });
      expect(result.text).toBe("private-output");
      const attributes = JSON.stringify(spans().map(span => span.attributes));
      expect(attributes).not.toContain("private-input");
      expect(attributes).not.toContain("private-output");
      expect(spans("LLM")).toHaveLength(1);
    } finally { masked.disable(); instrumentor.enable(); }
  });

  test("FI context session attributes reach operation and model spans", async () => {
    await context.with(setSession(context.active(), { sessionId: "session-vercel" }), () => sdk.generateText({ model: model(), prompt: "hi" }));
    expect(spans("LLM")[0].attributes[SESSION_ID]).toBe("session-vercel");
    expect(spans("CHAIN")[0].attributes[SESSION_ID]).toBe("session-vercel");
  });

  test("a broken tracing provider fails open without executing the model twice", async () => {
    const failure = new Error("broken telemetry");
    instrumentor.setTracerProvider({ getTracer: () => ({ startSpan() { throw failure; }, startActiveSpan() { throw failure; } }) } as any);
    const callModel = model();
    try {
      const result = await sdk.generateText({ model: callModel, prompt: "hi", maxRetries: 0 });
      expect(result.text).toBe("hello");
      expect(callModel.doGenerateCalls).toHaveLength(1);
      expect(spans()).toHaveLength(0);
    } finally { instrumentor.setTracerProvider(provider); }
  });

  test.each(["generateText", "generateObject", "embed", "embedMany"])("concurrent %s calls retain independent parent traces", async method => {
    const parents = [provider.getTracer("app").startSpan("parent-a"), provider.getTracer("app").startSpan("parent-b")];
    const schema = sdk.jsonSchema({ type: "object", properties: { hello: { type: "string" } }, required: ["hello"] });
    try {
      await Promise.all(parents.map((parent, i) => context.with(trace.setSpan(context.active(), parent), () =>
        sdk[method]({ model: method.startsWith("embed") ? embedding() : model(method === "generateObject" ? '{"hello":"world"}' : `result-${i}`),
          schema, value: `value-${i}`, values: [`value-${i}`], prompt: `prompt-${i}` }))));
      const models = spans(method.startsWith("embed") ? "EMBEDDING" : "LLM");
      expect(models).toHaveLength(2);
      expect(new Set(models.map(span => span.spanContext().traceId))).toEqual(new Set(parents.map(parent => parent.spanContext().traceId)));
    } finally { parents.forEach(parent => parent.end()); }
  });

  test("response clone and tee retain baseline byte semantics", async () => {
    const plain = baseline(() => original.streamText({ model: model(), prompt: "hi" })).toTextStreamResponse();
    const response = sdk.streamText({ model: model(), prompt: "hi" }).toTextStreamResponse();
    const clone = response.clone();
    expect(await response.text()).toBe(await plain.text());
    expect(await clone.text()).toBe("hello");
    expect(spans("LLM")).toHaveLength(1);
  });

  test("BYOB reader behavior matches the uninstrumented response body", async () => {
    const probe = async (result: any) => {
      const response = result.toTextStreamResponse();
      let reader: any;
      try { reader = response.body.getReader({ mode: "byob" }); }
      catch (error) { return { error: (error as Error).name, text: await response.text() }; }
      const bytes: number[] = [];
      while (true) {
        const item = await reader.read(new Uint8Array(64));
        if (item.value) bytes.push(...item.value);
        if (item.done) break;
      }
      reader.releaseLock();
      return { bytes };
    };
    const expected = await baseline(() => probe(original.streamText({ model: model(), prompt: "hi" })));
    const actual = await probe(sdk.streamText({ model: model(), prompt: "hi" }));
    expect(actual).toEqual(expected);
    expect(spans("LLM")).toHaveLength(1);
  });

  test("abort reason and provider invocation count match the uninstrumented SDK", async () => {
    const abort = new AbortController();
    const reason = new Error("consumer abort");
    abort.abort(reason);
    const run = async (api: any) => {
      const callModel = model();
      const value = await api.generateText({ model: callModel, prompt: "hi", abortSignal: abort.signal, maxRetries: 0 }).then((result: any) => result.text, (error: any) => error);
      return { value, calls: callModel.doGenerateCalls.length };
    };
    const expected = await baseline(() => run(original));
    const actual = await run(sdk);
    expect(actual.value).toBe(expected.value);
    expect(actual.calls).toBe(expected.calls);
  });

  test("repeated patching never duplicates model spans", async () => {
    sdk = instrumentor.manuallyInstrument(sdk, version);
    await sdk.generateText({ model: model(), prompt: "hi" });
    expect(spans("LLM")).toHaveLength(1);
  });

  if (major === 7) {
    test.each(["embed", "embedMany", "generateObject"])("v7 %s early validation rejection does not start an orphan span", async method => {
      const callModel = method === "generateObject" ? model() : embedding();
      const options = { model: callModel, prompt: "hi", value: "hi", values: ["hi"], dimensions: 0, maxRetries: -1 };
      const outcome = (api: any) => api[method](options).then(() => "unexpected success", (error: Error) => ({ name: error.name, message: error.message }));
      const expected = await baseline(() => outcome(original));
      expect(expected).not.toBe("unexpected success");
      expect(await outcome(sdk)).toEqual(expected);
      expect(spans()).toHaveLength(0);
      expect([...liveSpans.values()]).toEqual([]);
    });

    test("v7 dispatcher preserves local/global precedence, receivers and execute-wrapper results", async () => {
      const globalCallback = jest.fn();
      const globals = globalThis as any;
      globals.AI_SDK_TELEMETRY_INTEGRATIONS.push({ onStart: globalCallback });
      const output = { custom: "wrapper result" };
      const local = {
        onStart: jest.fn(function(this: unknown) { expect(this).toBe(local); }),
        executeLanguageModelCall: jest.fn(async function(this: unknown, { execute }: any) {
          expect(this).toBe(local);
          const result = await execute();
          return { ...result, providerMetadata: { test: output } };
        }),
      };
      try {
        const result = await sdk.generateText({ model: model(), prompt: "hi", telemetry: { integrations: local } });
        expect(result.providerMetadata.test).toBe(output);
        expect(local.onStart).toHaveBeenCalledTimes(1);
        expect(local.executeLanguageModelCall).toHaveBeenCalledTimes(1);
        expect(globalCallback).not.toHaveBeenCalled();
        expect(spans("LLM")).toHaveLength(1);
      } finally { globals.AI_SDK_TELEMETRY_INTEGRATIONS.pop(); }
    });

    test("v7 native global registry works without patching the SDK namespace", async () => {
      await original.generateText({ model: model(), prompt: "hi" });
      expect(spans("LLM")).toHaveLength(1);
    });
  }
});

describe("zero-code entry and return identity", () => {
  test("enable is zero-argument, idempotent and never replaces the provider", () => {
    const existing = trace.getTracerProvider();
    expect(enable()).toBe(enableVercelInstrumentation());
    expect(trace.getTracerProvider()).toBe(existing);
  });

  test("application-owned shimmer wrappers are chained, never unwrapped", () => {
    const instrumentor = new VercelAIInstrumentation();
    const value = Promise.resolve("application wrapper");
    const unwrap = jest.fn(() => { throw new Error("must not unwrap application code"); });
    const application = Object.assign(jest.fn(() => value), { __wrapped: true, __unwrap: unwrap });
    try {
      const patched = instrumentor.manuallyInstrument({ generateText: application }, "6.0.320");
      expect(patched.generateText({ model: {} })).toBe(value);
      expect(application).toHaveBeenCalledTimes(1);
      expect(unwrap).not.toHaveBeenCalled();
    } finally { instrumentor.disable(); }
  });

  test.each(["embed", "embedMany", "generateObject"])("v7 %s public rejected promise is returned without attaching handlers", async method => {
    const instrumentor = new VercelAIInstrumentation();
    const failure = new Error("unobserved SDK promise");
    const promise = Promise.reject(failure);
    // The test owns this handler; instrumentation must not install another one.
    const outcome = promise.catch(error => error);
    const then = jest.spyOn(promise, "then");
    const original = { [method]: jest.fn(() => promise) };
    const patched = instrumentor.manuallyInstrument(original, "7.0.158");
    try {
      expect(patched[method]({ model: {} })).toBe(promise);
      expect(then).not.toHaveBeenCalled();
      await expect(outcome).resolves.toBe(failure);
    } finally { instrumentor.disable(); }
  });

  test("unsupported majors are left untouched", () => {
    const instrumentor = new VercelAIInstrumentation({ instrumentationConfig: { enabled: false } });
    const module = { generateText: jest.fn() };
    expect(instrumentor.manuallyInstrument(module, "8.0.0")).toBe(module);
  });

  test.each(["5.0.283", "6.0.320", "7.0.158"])("%s wrappers preserve exact returns, this and thrown values", version => {
    const instrumentor = new VercelAIInstrumentation();
    const promise = Promise.resolve({ text: "identity" });
    const response = Object.freeze({ stream: new ReadableStream(), helper() { return this; } });
    const failure = {};
    const original = {
      generateText: jest.fn(function(this: any) { expect(this).toBe(original); return promise; }),
      streamText: jest.fn(() => response),
      generateObject() { throw failure; },
    };
    const patched = instrumentor.manuallyInstrument(original, version);
    try {
      expect(patched.generateText.call(original, { model: {} })).toBe(promise);
      expect(patched.streamText({ model: {} })).toBe(response);
      expect(() => patched.generateObject({ model: {} })).toThrow();
    } finally { instrumentor.disable(); }
  });
});
