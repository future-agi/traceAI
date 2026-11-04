import { CursorSDKInstrumentation } from "../instrumentation";
import { SDKMessageLike } from "../types";
import { drain, exportedText, memoryProvider, one, parentId } from "./helpers";

const input = "private-nested-input";
const output = "private-nested-output";
const errorText = "private-nested-error";
const result = { status: "finished", result: output };
type Callback = (this: unknown, ...args: any[]) => any;
type Options = { onDelta?: Callback; onStep?: Callback; [key: string]: unknown };
const tool = (id = "read-1") => ({ type: "read", callId: id, args: { path: input }, result: { status: "success", value: output } });
const started = (id = "read-1") => ({ update: { type: "tool-call-started", callId: id, toolCall: tool(id) } });
const completed = (id = "read-1") => ({ update: { type: "tool-call-completed", callId: id, toolCall: tool(id) } });
const step = (id = "read-1") => ({ step: { type: "toolCall", message: tool(id) } });
const streamEvents: SDKMessageLike[] = [
  { type: "tool_call", name: "read", call_id: "read-1", status: "running", args: { path: input } },
  { type: "tool_call", name: "read", call_id: "read-1", status: "completed", result: output },
];
const environments = ["FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_ERROR_DETAILS"];
let savedEnv: (string | undefined)[];
let instrumentation: CursorSDKInstrumentation;
const { provider, exporter } = memoryProvider();
const spans = () => exporter.getFinishedSpans();
beforeEach(() => {
  exporter.reset();
  savedEnv = environments.map(key => process.env[key]);
  environments.forEach(key => delete process.env[key]);
});
afterEach(async () => {
  await instrumentation?.forceFlush();
  instrumentation?.unpatch();
  instrumentation?.disable();
  environments.forEach((key, i) => { if (savedEnv[i] === undefined) delete process.env[key]; else process.env[key] = savedEnv[i]; });
});
afterAll(() => provider.shutdown());
function instrument(traceConfig?: { hideInputs?: boolean; hideOutputs?: boolean }) {
  instrumentation = new CursorSDKInstrumentation({ tracerProvider: provider, traceConfig, runEndGraceMs: 5 });
  return instrumentation;
}
function runHandle(events: SDKMessageLike[] = []) {
  return { id: "run-callback", ...result, async wait() { return result; }, async *stream() { yield* events; } };
}

it.each(["callback-only", "wait-only", "callback-plus-stream", "step-only"])("captures %s tools without consuming an extra stream", async mode => {
  let readers = 0;
  const received: string[] = [];
  const sdk = instrument();
  const agent = sdk.wrapAgent({
    agentId: "agent-callback",
    async send(_message: string, options?: Options) {
      if (mode !== "step-only") {
        await options?.onDelta?.(started());
        await options?.onDelta?.(completed());
      }
      await options?.onStep?.(step());
      return { ...runHandle(), async *stream() { readers++; yield* streamEvents; } };
    },
  });
  const run = await agent.send(input, mode === "wait-only" ? undefined : {
    onDelta({ update }) { received.push(update.type); }, onStep({ step: item }) { received.push(item.type); },
  });
  if (mode === "callback-plus-stream") await drain(run.stream());
  if (mode !== "callback-only") await run.wait();
  else await sdk.forceFlush();
  expect(readers).toBe(mode === "callback-plus-stream" ? 1 : 0);
  expect(one(spans(), "Cursor tool: read").attributes["gen_ai.tool.call.id"]).toBe("read-1");
  expect(parentId(one(spans(), "Cursor tool: read"))).toBe(one(spans(), "Cursor Agent run").spanContext().spanId);
  if (mode !== "wait-only") expect(received).toContain("toolCall");
  expect(exportedText(spans())).not.toContain(input);
  expect(exportedText(spans())).not.toContain(output);
});

it("preserves frozen options, callback receiver, all arguments, exact returned promise and call order", async () => {
  const receiver = {};
  const promise = Promise.resolve("application-return");
  const event = started();
  const calls: string[] = [];
  const callback: Callback = function (...args) {
    expect(this).toBe(receiver);
    expect(args).toEqual([event, "extra"]);
    calls.push("callback");
    return promise;
  };
  const options = Object.freeze({ onDelta: callback, marker: {} });
  const agent = instrument().wrapAgent({ async send(_message: string, observed?: Options) {
    expect(observed?.marker).toBe(options.marker);
    calls.push("before");
    expect(observed?.onDelta?.call(receiver, event, "extra")).toBe(promise);
    calls.push("after");
    return runHandle();
  } });
  await (await agent.send(input, options)).wait();
  expect(options.onDelta).toBe(callback);
  expect(calls).toEqual(["before", "callback", "after"]);
});

it.each(["throw", "reject"])("preserves callback %s identity and failure behavior", async mode => {
  const failure = new Error(errorText);
  const rejected = mode === "reject" ? Promise.reject(failure) : undefined;
  const callback: Callback = () => { if (mode === "throw") throw failure; return rejected; };
  const agent = instrument().wrapAgent({ async send(_message: string, options?: Options) {
    const pending = options?.onDelta?.(started());
    if (rejected) expect(pending).toBe(rejected);
    await pending;
    return runHandle();
  } });
  await expect(agent.send(input, { onDelta: callback })).rejects.toBe(failure);
  expect(one(spans(), "Cursor Agent run").status.code).toBe(2);
  expect(exportedText(spans())).not.toContain(errorText);
});

it("does not double count callback delta/step/stream text or usage", async () => {
  const usage = { inputTokens: 2, outputTokens: 3, totalTokens: 5 };
  const agent = instrument({ hideInputs: false, hideOutputs: false }).wrapAgent({ async send(_message: string, options?: Options) {
    options?.onDelta?.({ update: { type: "text-delta", text: output } });
    options?.onStep?.({ step: { type: "assistantMessage", message: { text: output } } });
    options?.onDelta?.({ update: { type: "turn-ended", usage } });
    return runHandle([
      { type: "assistant", message: { content: [{ type: "text", text: output }] } },
      { type: "usage", usage },
    ]);
  } });
  const run = await agent.send(input);
  await drain(run.stream()); await run.wait();
  expect(one(spans(), "Cursor assistant message").attributes["output.value"]).toBe(output);
  expect(one(spans(), "Cursor Agent run").attributes["gen_ai.usage.total_tokens"]).toBe(5);
});

const nested = (steps: unknown[]) => ({ status: "success", value: { conversationSteps: steps } });
async function traceTask(name: string, taskResult: unknown, traceConfig?: { hideInputs?: boolean; hideOutputs?: boolean }) {
  const events: SDKMessageLike[] = [
    { type: "tool_call", name, call_id: "outer-task", status: "running", args: { prompt: input } },
    { type: "tool_call", name, call_id: "outer-task", status: "completed", result: taskResult },
  ];
  const agent = instrument(traceConfig).wrapAgent({ async send() { return runHandle(events); } });
  const run = await agent.send(); await drain(run.stream()); await run.wait();
}

it.each(["task", "Task", "Agent"])("reconstructs %s -> subagent -> tools with scoped ids and deduplicated steps", async name => {
  const first = { type: "toolCall", message: tool("same-id") };
  const child = { type: "toolCall", message: { type: "Task", callId: "nested-task", result: nested([
    { type: "toolCall", message: tool("same-id") },
  ]) } };
  const steps: unknown[] = [first, first, { ...first }, child, { type: "assistantMessage", message: { text: output } }];
  // A cyclic task result must not recurse forever or emit duplicates.
  steps.push({ type: "toolCall", message: { type: "Agent", callId: "cycle", result: nested(steps) } });
  await traceTask(name, nested(steps));
  const root = one(spans(), "Cursor Agent run");
  const outer = spans().find(span => span.attributes["gen_ai.tool.call.id"] === "outer-task")!;
  expect(outer.name).toBe(`Cursor tool: ${name}`);
  const subagent = spans().find(span => span.name === "Cursor subagent" && parentId(span) === outer.spanContext().spanId);
  expect(subagent?.attributes["fi.span.kind"]).toBe("AGENT");
  const reads = spans().filter(span => span.name === "Cursor tool: read");
  expect(reads).toHaveLength(2);
  expect(parentId(reads[1])).not.toBe(parentId(reads[0]));
  expect(reads.some(span => parentId(span) === subagent?.spanContext().spanId)).toBe(true);
  for (const span of spans()) expect(span.spanContext().traceId).toBe(root.spanContext().traceId);
  expect(exportedText(spans())).not.toContain(input);
  expect(exportedText(spans())).not.toContain(output);
});

it.each([
  [true, true], [true, false], [false, true], [false, false],
])("respects independent nested content gates (hideInputs=%s, hideOutputs=%s)", async (hideInputs, hideOutputs) => {
  process.env.FI_HIDE_INPUTS = String(hideInputs);
  process.env.FI_HIDE_OUTPUTS = String(hideOutputs);
  await traceTask("task", nested([{ type: "toolCall", message: tool() }]));
  const exported = exportedText(spans());
  if (hideInputs) expect(exported).not.toContain(input); else expect(exported).toContain(input);
  if (hideOutputs) expect(exported).not.toContain(output); else expect(exported).toContain(output);
});

it("respects FI_HIDE_ERROR_DETAILS for nested errors even with content enabled", async () => {
  process.env.FI_HIDE_ERROR_DETAILS = "true";
  await traceTask("task", nested([{ type: "toolCall", message: {
    ...tool(), result: { status: "error", error: errorText },
  } }]), { hideInputs: false, hideOutputs: false });
  expect(one(spans(), "Cursor tool: read").status.code).toBe(2);
  expect(exportedText(spans())).not.toContain(errorText);
});

it("bounds wide/deep conversations and skips malformed steps without failing the caller", async () => {
  let deep: unknown = nested([{ type: "toolCall", message: tool("bottom") }]);
  for (let i = 0; i < 100; i++) deep = nested([{ type: "toolCall", message: { type: "task", callId: `deep-${i}`, result: deep } }]);
  const bad = { get type(): string { throw new Error("malformed"); } };
  const wide = Array.from({ length: 2000 }, (_, i) => ({ type: "toolCall", message: tool(`wide-${i}`) }));
  await traceTask("task", nested([null, {}, bad, { type: "toolCall", message: { type: "task", callId: "deep", result: deep } }, ...wide]));
  expect(spans().some(span => span.attributes["gen_ai.tool.call.id"] === "wide-0")).toBe(true);
  expect(spans().some(span => span.attributes["gen_ai.tool.call.id"] === "bottom")).toBe(false);
  expect(spans().length).toBeLessThan(1050);
  expect(one(spans(), "Cursor Agent run").status.code).toBe(1);
});

it("bounds cyclic, wide and deep protobuf tasks without losing valid siblings", async () => {
  const taskStep = (callId: string, steps: unknown[]) => ({ toolCall: {
    callId, taskToolCall: { result: { success: { conversationSteps: steps } } },
  } });
  let deep: unknown[] = [{ toolCall: { callId: "bottom", readToolCall: { args: { path: input }, result: { success: output } } } }];
  for (let i = 0; i < 100; i++) deep = [taskStep(`deep-${i}`, deep)];
  const steps: unknown[] = [null, { toolCall: { unknownToolCall: {} } }];
  steps.push(taskStep("cycle", steps), taskStep("deep", deep));
  for (let i = 0; i < 2000; i++) steps.push({ toolCall: {
    callId: `wide-${i}`, readToolCall: { args: { path: input }, result: { success: output } },
  } });
  await traceTask("task", nested(steps));
  expect(spans().some(span => span.attributes["gen_ai.tool.call.id"] === "wide-0")).toBe(true);
  expect(spans().some(span => span.attributes["gen_ai.tool.call.id"] === "bottom")).toBe(false);
  expect(spans().length).toBeLessThan(1050);
  expect(exportedText(spans())).not.toContain(input);
  expect(exportedText(spans())).not.toContain(output);
  expect(one(spans(), "Cursor Agent run").status.code).toBe(1);
});

it.each([true, false])("handles real SDK idless onStep snapshots (deltas=%s) without duplicate tools", async deltas => {
  const message = { type: "read", args: { path: input }, result: { status: "success", value: output } };
  const agent = instrument().wrapAgent({ async send(_message: string, options?: Options) {
    if (deltas) options?.onDelta?.(started());
    options?.onStep?.({ step: { type: "toolCall", message } });
    if (deltas) options?.onDelta?.(completed());
    return runHandle(streamEvents);
  } });
  const run = await agent.send(input);
  await drain(run.stream()); await run.wait();
  expect(one(spans(), "Cursor tool: read").status.code).toBe(1);
  if (deltas) expect(one(spans(), "Cursor tool: read").attributes["gen_ai.tool.call.id"]).toBe("read-1");
});

it("does not let a callback with missing usage suppress available stream totals", async () => {
  const agent = instrument().wrapAgent({ async send(_message: string, options?: Options) {
    options?.onDelta?.({ update: { type: "turn-ended" } });
    return runHandle([{ type: "usage", usage: { inputTokens: 2, outputTokens: 3, totalTokens: 5 } }]);
  } });
  const run = await agent.send(input);
  await drain(run.stream()); await run.wait();
  expect(one(spans(), "Cursor Agent run").attributes["gen_ai.usage.total_tokens"]).toBe(5);
});

it("deduplicates tools when the stream arrives before callbacks", async () => {
  const agent = instrument().wrapAgent({ async send(_message: string, options?: Options) {
    return { ...runHandle(), async *stream() {
      yield streamEvents[0];
      options?.onDelta?.(started());
      options?.onStep?.(step());
      options?.onDelta?.(completed());
      yield streamEvents[1];
    } };
  } });
  const run = await agent.send(input);
  await drain(run.stream()); await run.wait();
  expect(one(spans(), "Cursor tool: read").status.code).toBe(1);
  expect(one(spans(), "Cursor tool: read").attributes["gen_ai.tool.call.id"]).toBe("read-1");
});
