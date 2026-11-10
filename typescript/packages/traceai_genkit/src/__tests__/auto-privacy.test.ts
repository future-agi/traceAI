// Real Genkit, mock model, and current auto-agent boundary. No child processes/network.
import { context, trace } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor, type ReadableSpan, type SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { registerSecret, type TraceConfigOptions } from "@traceai/fi-core";
import { genkit, z } from "genkit";
import { mockModel } from "genkit/testing";
import { flushTracing } from "genkit/tracing";
import { privacyFilter } from "../../../traceai_auto/src/export-privacy";
import { enable, type GenkitAutoSpanProcessor } from "../index";

const AGENT_KEY = Symbol.for("traceai.auto.agent.v1");
const globals = globalThis as unknown as Record<symbol, unknown>;
const previousAgent = globals[AGENT_KEY];
const initialTerm = process.rawListeners("SIGTERM");
const initialInt = process.rawListeners("SIGINT");
const nativeExporter = new InMemorySpanExporter();
let exported: ReadableSpan[] = [];
const provider = new BasicTracerProvider({
  spanLimits: { attributeValueLengthLimit: 600 },
  spanProcessors: [new SimpleSpanProcessor(nativeExporter)],
});
const ai = genkit({});
const model = mockModel(ai, { name: "privacy/mock", respond: {
  text: "visible answer", usage: { inputTokens: 2, outputTokens: 3, totalTokens: 5 },
} });

// Match the real agent's identity-keyed onStart and use its actual final boundary.
// Full source-bootstrap coverage runs separately in contract/auto_bootstrap.cjs.
function addBranch(options: TraceConfigOptions & { hideLLMInvocationParameters?: boolean } = {}, activate = true) {
  const spans: ReadableSpan[] = [];
  const privacy = privacyFilter(options);
  const branch = new SimpleSpanProcessor({
    export(batch, done) { spans.push(...batch.map(span => privacy.span(span))); done({ code: 0 }); },
    async shutdown() {},
  });
  if (activate) expect(enable({ spanProcessor: branch as unknown as GenkitAutoSpanProcessor, traceConfig: options }).status).toBe("instrumented");
  (provider as unknown as { _activeSpanProcessor: { _spanProcessors: SpanProcessor[] } })._activeSpanProcessor._spanProcessors.push(branch);
  return { spans, branch };
}

beforeAll(() => {
  expect(context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable())).toBe(true);
  expect(trace.setGlobalTracerProvider(provider)).toBe(true);
  const primary = addBranch({ hideInputs: true }, false);
  exported = primary.spans;
  globals[AGENT_KEY] = { processor: primary.branch, config: {
    apiKey: "test-api-key", secretKey: "test-secret-key", traceConfig: { hideInputs: true },
  } };
});
beforeEach(() => { exported.length = 0; nativeExporter.reset(); });
afterAll(async () => {
  await flushTracing();
  await provider.shutdown();
  trace.disable();
  context.disable();
  for (const [signal, initial] of [["SIGTERM", initialTerm], ["SIGINT", initialInt]] as const) {
    for (const listener of process.rawListeners(signal)) if (!initial.includes(listener)) process.removeListener(signal, listener as (...args: unknown[]) => void);
  }
  if (previousAgent === undefined) delete globals[AGENT_KEY];
  else globals[AGENT_KEY] = previousAgent;
});

it("uses process credentials when enabled during an already-started real async Genkit flow", async () => {
  const token = "inflight-context-verify-12345";
  let entered!: () => void;
  let release!: () => void;
  const started = new Promise<void>(resolve => { entered = resolve; });
  const suspended = new Promise<void>(resolve => { release = resolve; });
  const flow = ai.defineFlow({ name: "inflightCredentialFlow", inputSchema: z.string(), outputSchema: z.string() }, async () => {
    entered();
    await suspended;
    return `response echoes ${token}`;
  });
  registerSecret(token);
  const running = flow("ordinary input");
  await started;
  try { expect(enable()).toEqual({ status: "instrumented" }); }
  finally { release(); }
  expect(await running).toBe(`response echoes ${token}`);
  await provider.forceFlush();
  expect(exported).toHaveLength(1);
  expect(exported[0].attributes["output.value"]).toContain("response echoes <redacted>");
  expect(JSON.stringify(exported)).not.toContain(token);
  const [native] = nativeExporter.getFinishedSpans();
  expect(native.attributes["genkit:output"]).toContain(token);
  expect(exported[0].spanContext()).toEqual(native.spanContext());
  expect(exported[0].startTime).toEqual(native.startTime);
  expect(exported[0].endTime).toEqual(native.endTime);
  expect(exported[0].duration).toEqual(native.duration);
});

it("fails closed on real truncated generate output with a duplicated hidden request", async () => {
  const marker = "HIDDEN_PROMPT_VERIFY_";
  expect((await ai.generate({ model, prompt: marker + "p".repeat(1000) })).text).toBe("visible answer");
  await provider.forceFlush();
  const native = nativeExporter.getFinishedSpans().find(span => span.name === "generate")!;
  const output = native.attributes["genkit:output"] as string;
  expect(output).toHaveLength(600);
  expect(output).toContain('"request"');
  expect(output).toContain(marker);
  expect(() => JSON.parse(output)).toThrow();
  const mapped = exported.find(span => span.spanContext().spanId === native.spanContext().spanId)!;
  expect(mapped.attributes["output.value"]).toBe("__REDACTED__");
  expect(mapped.attributes["output.mime_type"]).toBeUndefined();
  expect(JSON.stringify(exported)).not.toContain(marker);
  expect(exported).toHaveLength(nativeExporter.getFinishedSpans().length);
  const nativeModel = nativeExporter.getFinishedSpans().find(span => span.name === "privacy/mock")!;
  expect(exported.find(span => span.spanContext().spanId === nativeModel.spanContext().spanId)!.attributes["gen_ai.usage.total_tokens"]).toBe(5);
});

it.each([false, true])("uses registered credentials even when a real flow input is hidden or truncated (truncated=%s)", async truncated => {
  const token = "hidden-input-credential-12345";
  registerSecret(token);
  const flow = ai.defineFlow({ name: `hiddenCredentialFlow${truncated}`, inputSchema: z.object({ apiKey: z.string(), padding: z.string().optional() }), outputSchema: z.string() },
    async ({ apiKey }) => `response echoes ${apiKey}`);
  expect(await flow({ apiKey: token, ...(truncated ? { padding: "p".repeat(1000) } : {}) })).toBe(`response echoes ${token}`);
  await provider.forceFlush();
  expect(exported).toHaveLength(1);
  expect(exported[0].attributes["input.value"]).toBe("__REDACTED__");
  expect(exported[0].attributes["output.value"]).toContain("response echoes <redacted>");
  expect(JSON.stringify(exported)).not.toContain(token);
  expect(nativeExporter.getFinishedSpans()[0].attributes["genkit:input"]).toContain(token);
});

it("uses registered credentials after discarding real request-header metadata", async () => {
  const token = "context-header-credential-12345";
  registerSecret(token);
  const { spans, branch } = addBranch();
  const flow = ai.defineFlow({ name: "contextCredentialFlow", inputSchema: z.string(), outputSchema: z.string() }, async () => `response echoes ${token}`);
  expect(await flow("visible input", { context: { headers: { "x-api-key": token } } })).toBe(`response echoes ${token}`);
  await branch.forceFlush();
  expect(spans).toHaveLength(1);
  expect(spans[0].attributes["input.value"]).toContain("visible input");
  expect(spans[0].attributes["output.value"]).toContain("response echoes <redacted>");
  expect(spans[0].attributes["genkit:metadata:context"]).toBeUndefined();
  expect(JSON.stringify(spans)).not.toContain(token);
  expect(nativeExporter.getFinishedSpans()[0].attributes["genkit:metadata:context"]).toContain(token);
});

it.each(["input", "headers"])("protects registered credentials when truncated %s metadata omits the credential field", async source => {
  const token = `tail-${source}-credential-123456789`;
  registerSecret(token);
  // Observe truncated input on a sibling FI view before the hiding view exports.
  // Do not depend on a non-hiding branch left behind by an earlier test.
  const observing = source === "input" ? addBranch() : undefined;
  const { spans, branch } = addBranch(source === "input" ? { hideInputs: true } : {});
  const flow = ai.defineFlow({ name: `tailCredential${source}`, inputSchema: z.any(), outputSchema: z.string() },
    async () => `echo ${token}`);
  const result = source === "input"
    ? await flow({ padding: "p".repeat(1000), apiKey: token })
    : await flow("ordinary input", { context: { headers: { "x-padding": "p".repeat(1000), "x-api-key": token } } });
  expect(result).toBe(`echo ${token}`);
  await branch.forceFlush();
  const [native] = nativeExporter.getFinishedSpans();
  const evidence = native.attributes[source === "input" ? "genkit:input" : "genkit:metadata:context"] as string;
  expect(evidence).toHaveLength(600);
  expect(evidence).not.toContain(token);
  expect(() => JSON.parse(evidence)).toThrow();
  expect(spans).toHaveLength(1);
  expect(JSON.stringify(spans)).not.toContain(token);
  if (observing) {
    expect(observing.spans).toHaveLength(1);
    expect(observing.spans[0].attributes["traceai.capture.host_data_unknown"]).toBe(true);
    // Core's sticky loss contract withholds all content, not just the lost field.
    expect(spans[0].attributes["traceai.capture.host_data_unknown"]).toBe(true);
    expect(spans[0].attributes["output.value"]).toBe("<unknown: host-truncated>");
    expect(spans[0].name).toBe("<unknown: host-truncated>");
  } else {
    // This stand-in drops header context before core can observe its truncation.
    expect(spans[0].attributes["output.value"]).toContain("echo <redacted>");
    expect(spans[0].attributes["traceai.capture.host_data_unknown"]).toBeUndefined();
  }
  expect(spans[0].spanContext()).toEqual(native.spanContext());
  expect(spans[0].startTime).toEqual(native.startTime);
  expect(spans[0].endTime).toEqual(native.endTime);
  expect(spans[0].duration).toEqual(native.duration);

  // A separate complete span still proves registry redaction without relying
  // on blanket withholding: its input and context never contain the token.
  expect(await flow("credential-free input")).toBe(`echo ${token}`);
  await branch.forceFlush();
  expect(spans).toHaveLength(2);
  expect(spans[1].attributes["traceai.capture.host_data_unknown"]).toBeUndefined();
  expect(spans[1].attributes["output.value"]).toContain("echo <redacted>");
  expect(JSON.stringify(spans)).not.toContain(token);
});

it.each([false, true])("scrubs earlier descendants immediately via the registry (native intermediate=%s)", async intermediate => {
  const token = `ancestor-credential-${intermediate}-123456789`;
  registerSecret(token);
  let childEnded = false;
  const tool = ai.defineTool({ name: `ancestorEchoTool${intermediate}`, description: "echo", inputSchema: z.string(), outputSchema: z.string() },
    async () => `echo ${token}`);
  const invoke = () => tool("credential-free input");
  const flow = ai.defineFlow({ name: `ancestorCredentialFlow${intermediate}`, inputSchema: z.object({ apiKey: z.string() }), outputSchema: z.string() }, async () => {
    const result = intermediate
      ? await provider.getTracer("application").startActiveSpan("native intermediate", async span => {
        try { return await invoke(); } finally { span.end(); }
      })
      : await invoke();
    childEnded = nativeExporter.getFinishedSpans().some(span => span.name === `ancestorEchoTool${intermediate}`);
    // The process registry already protects each ended child: no root buffering.
    expect(exported).toHaveLength(intermediate ? 2 : 1);
    return result;
  });
  expect(await flow({ apiKey: token })).toBe(`echo ${token}`);
  await provider.forceFlush();
  expect(childEnded).toBe(true);
  expect(exported).toHaveLength(intermediate ? 3 : 2);
  expect(JSON.stringify(exported)).not.toContain(token);
  expect(exported.find(span => span.name === `ancestorEchoTool${intermediate}`)!.attributes["output.value"]).toContain("echo <redacted>");
  for (const span of exported) {
    const native = nativeExporter.getFinishedSpans().find(item => item.spanContext().spanId === span.spanContext().spanId)!;
    expect(span.spanContext()).toEqual(native.spanContext());
    expect(span.startTime).toEqual(native.startTime);
    expect(span.endTime).toEqual(native.endTime);
    expect(span.duration).toEqual(native.duration);
  }
});

it.each([
  ["defaults", false, undefined], ["environment", true, undefined],
  ["option", true, true], ["option override", false, false],
] as const)("separates invocation parameters from both real generation inputs (%s)", async (mode, hide, option) => {
  const previous = process.env.FI_HIDE_LLM_INVOCATION_PARAMETERS;
  process.env.FI_HIDE_LLM_INVOCATION_PARAMETERS = mode === "defaults" ? "false" : "true";
  const config = { temperature: 0.314159, topP: 0.271828, maxOutputTokens: 37 };
  try {
    const { spans, branch } = addBranch(option === undefined ? {} : { hideLLMInvocationParameters: option });
    expect((await ai.generate({ model, prompt: "visible prompt", config })).text).toBe("visible answer");
    await branch.forceFlush();
    expect(spans).toHaveLength(2);
    for (const span of spans) {
      const input = JSON.parse(span.attributes["input.value"] as string);
      expect(input.config).toBeUndefined();
      expect(JSON.stringify(input)).toContain("visible prompt");
      if (hide) expect(span.attributes["gen_ai.request.parameters"]).toBeUndefined();
      else expect(JSON.parse(span.attributes["gen_ai.request.parameters"] as string)).toEqual(config);
    }
    const encoded = JSON.stringify(spans);
    if (hide) for (const key of ["temperature", "0.314159", "topP", "0.271828", "maxOutputTokens"]) expect(encoded).not.toContain(key);
    for (const native of nativeExporter.getFinishedSpans()) expect(JSON.parse(native.attributes["genkit:input"] as string).config).toEqual(config);
  } finally {
    if (previous === undefined) delete process.env.FI_HIDE_LLM_INVOCATION_PARAMETERS;
    else process.env.FI_HIDE_LLM_INVOCATION_PARAMETERS = previous;
  }
});

it("protects descendants started after activation inside an already-running real flow", async () => {
  const token = "late-parent-child-credential-123456789";
  registerSecret(token);
  const { spans, branch } = addBranch({ hideInputs: true }, false);
  let entered!: () => void;
  let release!: () => void;
  const started = new Promise<void>(resolve => { entered = resolve; });
  const suspended = new Promise<void>(resolve => { release = resolve; });
  const tool = ai.defineTool({ name: "lateParentTool", description: "echo", inputSchema: z.string(), outputSchema: z.string() }, async () => `echo ${token}`);
  const flow = ai.defineFlow({ name: "lateParentFlow", inputSchema: z.object({ apiKey: z.string() }), outputSchema: z.string() }, async () => {
    entered(); await suspended;
    const result = await tool("ordinary input");
    expect(spans).toHaveLength(1);
    return result;
  });
  const running = flow({ apiKey: token });
  await started;
  try { expect(enable({ spanProcessor: branch, traceConfig: { hideInputs: true } }).status).toBe("instrumented"); }
  finally { release(); }
  expect(await running).toBe(`echo ${token}`);
  await branch.forceFlush();
  expect(spans).toHaveLength(2);
  for (const span of spans) expect(span.attributes["output.value"]).toContain("echo <redacted>");
  expect(JSON.stringify(spans)).not.toContain(token);
});

it("protects concurrent real flows through one registry without root export delays", async () => {
  const tokens = ["concurrent-first-credential-123456789", "concurrent-second-credential-123456789"];
  for (const token of tokens) registerSecret(token);
  let entered!: () => void;
  let release!: () => void;
  const started = new Promise<void>(resolve => { entered = resolve; });
  const suspended = new Promise<void>(resolve => { release = resolve; });
  const tool = ai.defineTool({ name: "concurrentEchoTool", description: "echo", inputSchema: z.number(), outputSchema: z.string() }, async id => `echo ${tokens[id]}`);
  const flow = ai.defineFlow({ name: "concurrentCredentialFlow", inputSchema: z.object({ id: z.number(), apiKey: z.string() }), outputSchema: z.string() }, async ({ id }) => {
    const result = await tool(id);
    if (id === 1) { entered(); await suspended; }
    return result;
  });
  const second = flow({ id: 1, apiKey: tokens[1] });
  await started;
  try {
    expect(exported).toHaveLength(1);
    expect(await flow({ id: 0, apiKey: tokens[0] })).toBe(`echo ${tokens[0]}`);
    expect(exported).toHaveLength(3);
    for (const span of exported) expect(span.attributes["output.value"]).toContain("echo <redacted>");
  } finally { release(); }
  expect(await second).toBe(`echo ${tokens[1]}`);
  await provider.forceFlush();
  expect(exported).toHaveLength(4);
  for (const token of tokens) expect(JSON.stringify(exported)).not.toContain(token);
});

it("retains legitimate flow and tool request fields instead of treating them as generate's input", async () => {
  const { spans, branch } = addBranch();
  const result = { request: { id: 7 }, status: "accepted" };
  const tool = ai.defineTool({ name: "requestResultTool", description: "request result", inputSchema: z.string(), outputSchema: z.any() }, async () => result);
  const flow = ai.defineFlow({ name: "requestResultFlow", inputSchema: z.string(), outputSchema: z.any() }, async input => tool(input));
  expect(await flow("hello")).toEqual(result);
  await branch.forceFlush();
  expect(spans).toHaveLength(2);
  for (const span of spans) expect(JSON.parse(span.attributes["output.value"] as string)).toEqual(result);
});

it("leaves explicit hideErrorDetails=false to the owning sanitizer for real flow errors", async () => {
  const { spans, branch } = addBranch({ hideInputs: true, hideErrorDetails: false });
  const failure = new Error("ordinary diagnostic detail");
  const flow = ai.defineFlow({ name: "diagnosticFlow", inputSchema: z.string(), outputSchema: z.string() }, async () => { throw failure; });
  await expect(flow("hidden input")).rejects.toBe(failure);
  await branch.forceFlush();
  expect(spans).toHaveLength(1);
  expect(spans[0].status.message).toBe("ordinary diagnostic detail");
  expect(spans[0].events.find(event => event.name === "exception")!.attributes!["exception.message"]).toBe("ordinary diagnostic detail");
  expect(spans[0].events.find(event => event.name === "exception")!.attributes!["exception.stacktrace"]).toBeUndefined();
});
