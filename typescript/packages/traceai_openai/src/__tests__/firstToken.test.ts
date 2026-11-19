import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OpenAIInstrumentation } from "../instrumentation";

const TTFT = "gen_ai.response.time_to_first_token";
type API = "chat" | "completion" | "responses";
type Step = { after: number; value: any };
const chat = (delta: object, finish: string | null = null) => ({
  id: "chat-timing-fixture", object: "chat.completion.chunk", created: 1,
  model: "fixture-model", choices: [{ index: 0, delta, finish_reason: finish }],
});
const usage = { prompt_tokens: 7, completion_tokens: 3, total_tokens: 10 };
const terminal = {
  type: "response.completed", response: {
    id: "response-timing-fixture", object: "response", status: "completed", model: "fixture-model",
    output: [{ type: "message", role: "assistant", content: [{ type: "output_text", text: "visible answer" }] }],
    usage: { input_tokens: 7, output_tokens: 3, total_tokens: 10 },
  },
};

// No network, module mocks, sleeps or SDK-version-specific transport internals.
// Exercise the real instrumentation, FITracer, iterator lifecycle and exporter.
describe("OpenAI first generated-content timing", () => {
  let exporter: InMemorySpanExporter;
  let provider: BasicTracerProvider;
  let instrumentation: OpenAIInstrumentation;

  beforeEach(() => {
    jest.useFakeTimers({ doNotFake: ["Date", "hrtime", "nextTick", "setImmediate", "queueMicrotask"] });
    exporter = new InMemorySpanExporter();
    provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  });
  afterEach(async () => {
    instrumentation?.disable();
    await provider.shutdown();
    jest.useRealTimers();
  });

  function fixture(api: API, steps: Step[], options: {
    legacy?: boolean; error?: Error; hideContent?: boolean; immediate?: any; dispatchMs?: number;
  } = {}) {
    const controller = new AbortController();
    let pulls = 0;
    const iterator = async function* () {
      for (const step of steps) {
        pulls++;
        jest.advanceTimersByTime(step.after);
        yield step.value;
      }
      if (options.error) throw options.error;
    };
    const original: any = options.legacy
      ? { controller, [Symbol.asyncIterator]: iterator }
      : { controller, iterator, [Symbol.asyncIterator]() { return this.iterator(); } };
    const create = () => {
      // Simulates time spent awaiting the provider before the SDK returns a stream.
      jest.advanceTimersByTime(options.dispatchMs ?? 120);
      return Promise.resolve(options.immediate ?? original);
    };
    class Chat { create() { return create(); } }
    class Completion { create() { return create(); } }
    class Responses { create() { return create(); } }
    class Embeddings { create() { return Promise.resolve({ data: [] }); } }
    const module = { OpenAI: { Chat: { Completions: Chat }, Completions: Completion, Responses, Embeddings } };
    instrumentation = new OpenAIInstrumentation({
      instrumentationConfig: { enabled: false },
      traceConfig: options.hideContent ? { hideInputs: true, hideOutputs: true } : {},
    });
    instrumentation.setTracerProvider(provider);
    instrumentation.manuallyInstrument(module as any);
    const request = { model: "fixture-model", stream: options.immediate === undefined,
      ...(api === "chat" ? { messages: [{ role: "user", content: "private timing input" }] }
        : api === "completion" ? { prompt: "private timing input" } : { input: "private timing input" }),
    };
    const resource: any = api === "chat" ? new Chat() : api === "completion" ? new Completion() : new Responses();
    return { original, controller, request, call: () => resource.create(request), pulls: () => pulls };
  }

  async function drain(stream: any) {
    const values: any[] = [];
    for await (const value of stream) values.push(value);
    return values;
  }

  test.each([false, true])("RED: request-to-first-content seconds include dispatch wait; legacy=%s", async legacy => {
    const steps = [
      { after: 30, value: chat({ role: "assistant", content: "" }) },
      { after: 100, value: chat({ content: "visible answer" }) },
      { after: 400, value: chat({ content: " later" }) },
      { after: 100, value: { ...chat({}, "stop"), usage } },
    ];
    const f = fixture("chat", steps, { legacy });
    const stream = await f.call();
    expect(stream).toBe(f.original);
    expect(stream.controller).toBe(f.controller);
    expect(f.pulls()).toBe(0); // no eager tee or read
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    expect(await drain(stream)).toEqual(steps.map(step => step.value));
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    // Base c622516 emits undefined here; first role chunk would incorrectly be .15.
    // Starting the clock at stream resolution would incorrectly be .13.
    expect(spans[0].attributes[TTFT]).toBeCloseTo(0.25, 9);
    expect(spans[0].attributes).toMatchObject({
      "gen_ai.request.model": "fixture-model", "gen_ai.usage.input_tokens": 7,
      "gen_ai.usage.output_tokens": 3, "output.value": "visible answer later",
    });
    expect(JSON.stringify(f.request)).toContain("private timing input");
  });

  test.each<{ label: string; api: API; value: any }>([
    { label: "chat text", api: "chat", value: chat({ content: "visible answer" }) },
    { label: "chat refusal", api: "chat", value: chat({ refusal: "cannot comply" }) },
    { label: "chat reasoning", api: "chat", value: chat({ reasoning_content: "reason" }) },
    { label: "chat audio", api: "chat", value: chat({ audio: { data: "QUJD" } }) },
    { label: "chat tool name", api: "chat", value: chat({ tool_calls: [{ index: 0, function: { name: "lookup" } }] }) },
    { label: "chat tool arguments", api: "chat", value: chat({ tool_calls: [{ index: 0, function: { arguments: "{}" } }] }) },
    { label: "NCG-1 chat custom name only", api: "chat", value: chat({ tool_calls: [{ index: 0, type: "custom", custom: { name: "code_exec" } }] }) },
    { label: "NCG-1 chat custom input only", api: "chat", value: chat({ tool_calls: [{ index: 0, type: "custom", custom: { input: "print(" } }] }) },
    { label: "legacy function", api: "chat", value: chat({ function_call: { name: "lookup" } }) },
    { label: "legacy completion", api: "completion", value: { id: "completion-fixture", model: "fixture-model", choices: [{ index: 0, text: "visible answer" }] } },
    { label: "responses text", api: "responses", value: { type: "response.output_text.delta", delta: "visible answer" } },
    { label: "responses reasoning", api: "responses", value: { type: "response.reasoning_summary_text.delta", delta: "reason" } },
    { label: "responses refusal", api: "responses", value: { type: "response.refusal.delta", delta: "cannot comply" } },
    { label: "responses tool arguments", api: "responses", value: { type: "response.function_call_arguments.delta", delta: "{}" } },
    { label: "responses audio", api: "responses", value: { type: "response.output_audio.delta", delta: "QUJD" } },
    { label: "responses tool start", api: "responses", value: { type: "response.output_item.added", item: { type: "function_call", name: "lookup", arguments: "" } } },
  ])("RED: $label times a generated delta before response retention/aggregation", async ({ api, value }) => {
    const last = api === "responses" ? terminal : api === "chat" ? { ...chat({}, "stop"), usage }
      : { id: "completion-fixture", model: "fixture-model", choices: [{ index: 0, text: "", finish_reason: "stop" }], usage };
    const f = fixture(api, [{ after: 130, value }, { after: 600, value: last }]);
    expect(await drain(await f.call())).toEqual([value, last]);
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].attributes[TTFT]).toBeCloseTo(0.25, 9);
  });

  // The custom-only table cases above are absent on r1. These mixed cases would
  // instead report .55 on r1: the first later text/function delta, not the custom
  // name/input at .25. Also exercise continuation deltas that omit id and type.
  test.each([
    { label: "name before text", custom: { name: "code_exec" }, later: { content: "later text" }, legacy: false },
    { label: "input before function", custom: { input: "print(" }, later: { tool_calls: [{ index: 1, type: "function", function: { name: "lookup" } }] }, legacy: true },
  ])("RED NCG-1: custom $label is the first token", async ({ custom, later, legacy }) => {
    const steps = [
      { after: 20, value: chat({ role: "assistant" }) },
      { after: 20, value: chat({ tool_calls: [{ index: 0, id: "custom-fixture", type: "custom", custom: { name: "", input: "" } }] }) },
      { after: 90, value: chat({ tool_calls: [{ index: 0, custom }] }) },
      { after: 300, value: chat(later) },
      { after: 200, value: chat({ tool_calls: [{ index: 0, custom: { input: "1)" } }] }) },
      { after: 100, value: { ...chat({}, "tool_calls"), usage } },
    ];
    const f = fixture("chat", steps, { legacy });
    const stream = await f.call();
    expect(stream).toBe(f.original);
    expect(stream.controller).toBe(f.controller);
    expect(f.pulls()).toBe(0);
    const values = await drain(stream);
    values.forEach((value, index) => expect(value).toBe(steps[index].value));
    expect(values).toHaveLength(steps.length);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].attributes[TTFT]).toBeCloseTo(0.25, 9);
  });

  test.each<API>(["chat", "completion", "responses"])("no fabricated token timestamp for %s control-only streams", async api => {
    const values = api === "responses"
      ? [{ type: "response.created", response: { id: "fixture" } },
        { type: "response.output_item.added", item: { type: "message", id: "fixture", role: "assistant", content: [] } },
        { type: "response.output_text.delta", delta: "" }, terminal]
      : api === "chat"
        ? [chat({ role: "assistant" }), chat({ content: "", tool_calls: [{ index: 0, id: "call-fixture", type: "function" }] }), { ...chat({}, "stop"), usage }]
        : [{ id: "fixture", choices: [{ index: 0, text: "" }], usage }];
    const f = fixture(api, values.map(value => ({ after: 20, value })));
    await drain(await f.call());
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].attributes[TTFT]).toBeUndefined();
  });

  test("NCG-1: custom identity and empty payloads are not generated content", async () => {
    const values = [
      chat({ tool_calls: [{ index: 0, id: "custom-fixture", type: "custom" }] }),
      chat({ tool_calls: [{ index: 0, custom: {} }] }),
      chat({ tool_calls: [{ index: 0, custom: { name: "", input: "" } }] }),
      { ...chat({}, "tool_calls"), usage },
    ];
    const f = fixture("chat", values.map(value => ({ after: 20, value })));
    expect(await drain(await f.call())).toEqual(values);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].attributes[TTFT]).toBeUndefined();
  });

  test.each([false, true])("early return retains only actually observed timing; content=%s", async content => {
    const f = fixture("chat", [
      { after: 130, value: chat(content ? { content: "visible answer" } : { role: "assistant" }) },
      { after: 400, value: chat({ content: "not read" }) },
    ]);
    for await (const _chunk of await f.call()) break;
    expect(f.pulls()).toBe(1);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].attributes[TTFT]).toBe(content ? 0.25 : undefined);
  });

  test("RED: a zero-time first token is not overwritten by later chunks", async () => {
    const f = fixture("chat", [
      { after: 0, value: chat({ content: "first" }) },
      { after: 500, value: chat({ content: " later" }) },
      { after: 100, value: { ...chat({}, "stop"), usage } },
    ], { dispatchMs: 0 });
    await drain(await f.call());
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].attributes[TTFT]).toBe(0);
  });

  test("a transport rejection keeps identity and does not invent timing", async () => {
    const error = new Error("synthetic stream failure");
    const f = fixture("chat", [{ after: 30, value: chat({ role: "assistant" }) }], { error });
    await expect(drain(await f.call())).rejects.toBe(error);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].attributes[TTFT]).toBeUndefined();
  });

  test("abort before consumption leaves timing absent and ends once", async () => {
    const f = fixture("chat", [{ after: 130, value: chat({ content: "not read" }) }]);
    await f.call();
    f.controller.abort();
    expect(f.pulls()).toBe(0);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].attributes[TTFT]).toBeUndefined();
  });

  test("nonstream results have no streaming metric", async () => {
    const f = fixture("chat", [], { immediate: {
      id: "nonstream-fixture", model: "fixture-model", choices: [{ index: 0, message: { role: "assistant", content: "answer" }, finish_reason: "stop" }], usage,
    } });
    await f.call();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].attributes[TTFT]).toBeUndefined();
  });

  test("RED: timing never reintroduces hidden input or output payloads", async () => {
    const f = fixture("chat", [
      { after: 130, value: chat({ content: "private timing output" }) },
      { after: 100, value: { ...chat({}, "stop"), usage } },
    ], { hideContent: true });
    await drain(await f.call());
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].attributes[TTFT]).toBeCloseTo(0.25, 9);
    expect(JSON.stringify(spans[0].attributes)).not.toContain("private timing input");
    expect(JSON.stringify(spans[0].attributes)).not.toContain("private timing output");
    expect(JSON.stringify(f.request)).toContain("private timing input");
  });

  test("RED NCG-1: custom timing does not bypass hidden content", async () => {
    const custom = { name: "private_custom_name", input: "private custom input" };
    const value = chat({ tool_calls: [{ index: 0, type: "custom", custom }] });
    const f = fixture("chat", [
      { after: 130, value },
      { after: 100, value: { ...chat({}, "tool_calls"), usage } },
    ], { hideContent: true });
    const values = await drain(await f.call());
    expect(values[0]).toBe(value);
    expect(values[0].choices[0].delta.tool_calls[0].custom).toBe(custom);
    expect(custom).toEqual({ name: "private_custom_name", input: "private custom input" });
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].attributes[TTFT]).toBeCloseTo(0.25, 9);
    expect(JSON.stringify(spans[0].attributes)).not.toContain("private timing input");
    expect(JSON.stringify(spans[0].attributes)).not.toContain(custom.name);
    expect(JSON.stringify(spans[0].attributes)).not.toContain(custom.input);
  });
});
