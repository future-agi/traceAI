import { SpanStatusCode, context } from "@opentelemetry/api";
import { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { setSession } from "@traceai/fi-core";
import { CursorAttributes as C, SpanNames, StandardAttributes as S } from "../attributes";
import { CursorSDKInstrumentation, resolveContentPolicy, shutdown } from "../instrumentation";
import { isPatched } from "../wrappers";
import {
  AGENT_ID,
  LIVE_EDIT_ID,
  LIVE_READ_ID,
  LIVE_USAGE,
  PLAN_ERROR,
  PROMPT_MARKER,
  PROMPT_TOKENS,
  SECRET_ARG,
  SECRET_RESULT,
  THINKING_TEXT,
  USAGE,
  events,
  failedRun,
  fullRun,
  liveModelUnavailableRun,
  liveParallelToolsRun,
  liveRun,
  slowRun,
} from "./fixtures/events";
import { FakeAuthenticationError, FakeRun, Step, makeFakeSdk } from "./fixtures/fakeSdk";
import { StackContextManager, allAttributeText, attr, byName, drain, exportedText, memoryProvider, one, parentId, sleep, until } from "./helpers";

const { provider, exporter } = memoryProvider();
const instrumentations: CursorSDKInstrumentation[] = [];

function instrument(
  script: Parameters<typeof makeFakeSdk>[0],
  options: { createError?: Error; traceConfig?: { hideInputs?: boolean; hideOutputs?: boolean }; graceMs?: number; drainMs?: number } = {},
) {
  const sdk = makeFakeSdk(script, options.createError);
  const instrumentation = new CursorSDKInstrumentation({
    tracerProvider: provider,
    traceConfig: options.traceConfig,
    runEndGraceMs: options.graceMs ?? 30,
    streamDrainTimeoutMs: options.drainMs,
  });
  instrumentations.push(instrumentation);
  expect(instrumentation.manuallyInstrument(sdk)).toBe(true);
  return { sdk, instrumentation };
}

const spans = () => exporter.getFinishedSpans();
const runSpans = () => byName(spans(), SpanNames.RUN);
const toolSpans = () => spans().filter((s) => s.name.startsWith(SpanNames.TOOL_PREFIX));
const childrenOf = (span: ReadableSpan) => spans().filter((s) => parentId(s) === span.spanContext().spanId);

const ENV_KEYS = ["FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS"] as const;
const savedEnv: Record<string, string | undefined> = {};

beforeEach(() => {
  exporter.reset();
  for (const key of ENV_KEYS) {
    savedEnv[key] = process.env[key];
    delete process.env[key];
  }
});

afterEach(() => {
  for (const key of ENV_KEYS) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
  while (instrumentations.length) {
    const instrumentation = instrumentations.pop()!;
    instrumentation.unpatch();
    instrumentation.disable();
  }
});

describe("streamed run (documented events)", () => {
  async function streamFullRun(traceConfig?: { hideInputs?: boolean; hideOutputs?: boolean }) {
    const { sdk } = instrument({ runs: [fullRun()] }, { traceConfig });
    const agent = await sdk.Agent.create({ apiKey: "crsr_unit_key", model: { id: "composer-2" }, local: { cwd: "/tmp" }, agentId: AGENT_ID });
    const run = await agent.send(`List files ${PROMPT_MARKER}`);
    const seen = await drain(run.stream());
    const result = await run.wait();
    await until(() => runSpans().length === 1);
    return { agent, run, seen, result };
  }

  it("builds run -> thinking / assistant / tool spans with session, model, tokens and status", async () => {
    const { seen, result } = await streamFullRun();
    expect(seen).toHaveLength(19);
    expect(result.status).toBe("finished");

    const run = one(spans(), SpanNames.RUN);
    expect(run.status.code).toBe(SpanStatusCode.OK);
    expect(attr(run, S.FI_SPAN_KIND)).toBe("AGENT");
    expect(attr(run, S.SESSION_ID)).toBe(AGENT_ID);
    expect(attr(run, S.CONVERSATION_ID)).toBe(AGENT_ID);
    expect(attr(run, C.AGENT_ID)).toBe(AGENT_ID);
    expect(attr(run, C.RUNTIME)).toBe("local");
    expect(attr(run, C.RUN_ID)).toBe("run-1");
    expect(attr(run, C.REQUEST_ID)).toBe("req-1");
    expect(attr(run, C.RUN_STATUS)).toBe("finished");
    expect(attr(run, C.LAST_STATUS_EVENT)).toBe("FINISHED");
    expect(attr(run, S.MODEL_NAME)).toBe("composer-2");
    expect(attr(run, C.TOOLS)).toEqual(["read", "ls", "shell"]);
    expect(attr(run, S.INPUT_TOKENS)).toBe(PROMPT_TOKENS);
    expect(attr(run, C.USAGE_INPUT_TOKENS)).toBe(USAGE.inputTokens);
    expect(attr(run, S.OUTPUT_TOKENS)).toBe(USAGE.outputTokens);
    expect(attr(run, S.TOTAL_TOKENS)).toBe(USAGE.totalTokens);
    expect(attr(run, S.CACHE_READ_TOKENS)).toBe(USAGE.cacheReadTokens);
    expect(attr(run, S.CACHE_WRITE_TOKENS)).toBe(USAGE.cacheWriteTokens);
    expect(attr(run, S.REASONING_TOKENS)).toBe(USAGE.reasoningTokens);
    expect(attr(run, C.USAGE_SOURCE)).toBe("run");
    expect(typeof attr(run, C.RUN_DURATION_MS)).toBe("number");
    expect(attr(run, C.INPUT_LENGTH)).toBe(`List files ${PROMPT_MARKER}`.length);

    const children = childrenOf(run);
    expect(children.map((s) => s.name).sort()).toEqual(
      [
        SpanNames.ASSISTANT_MESSAGE,
        SpanNames.ASSISTANT_MESSAGE,
        SpanNames.THINKING,
        `${SpanNames.TOOL_PREFIX}ls`,
        `${SpanNames.TOOL_PREFIX}read`,
        `${SpanNames.TOOL_PREFIX}shell`,
      ].sort(),
    );
    for (const child of children) {
      expect(child.spanContext().traceId).toBe(run.spanContext().traceId);
      expect(attr(child, S.SESSION_ID)).toBe(AGENT_ID);
    }

    const ls = one(spans(), `${SpanNames.TOOL_PREFIX}ls`);
    expect(attr(ls, S.FI_SPAN_KIND)).toBe("TOOL");
    expect(attr(ls, S.TOOL_NAME)).toBe("ls");
    expect(attr(ls, S.TOOL_CALL_ID)).toBe("call-ls");
    expect(attr(ls, C.TOOL_STATUS)).toBe("completed");
    expect(attr(ls, C.TOOL_STARTED_EVENT)).toBe(true);
    expect(attr(ls, C.TOOL_RESULT_TRUNCATED)).toBe(false);
    expect(ls.status.code).toBe(SpanStatusCode.OK);

    const read = one(spans(), `${SpanNames.TOOL_PREFIX}read`);
    expect(read.status.code).toBe(SpanStatusCode.ERROR);
    expect(read.status.message).toBe("read failed");

    const shell = one(spans(), `${SpanNames.TOOL_PREFIX}shell`);
    expect(attr(shell, C.TOOL_STARTED_EVENT)).toBe(false);
    expect(shell.status.code).toBe(SpanStatusCode.OK);

    const thinking = one(spans(), SpanNames.THINKING);
    expect(attr(thinking, S.FI_SPAN_KIND)).toBe("CHAIN");
    expect(attr(thinking, C.THINKING_DURATION_MS)).toBe(420);
    expect(attr(thinking, C.TEXT_LENGTH)).toBe(THINKING_TEXT.length);

    const messages = byName(spans(), SpanNames.ASSISTANT_MESSAGE);
    expect(messages.map((m) => attr(m, C.TEXT_LENGTH))).toEqual(["Let me look.".length, `README says ${SECRET_RESULT}`.length]);
    for (const m of messages) {
      expect(attr(m, S.FI_SPAN_KIND)).toBe("LLM");
      expect(attr(m, S.MODEL_NAME)).toBe("composer-2");
    }

    // Children start and end inside the run span.
    const toMs = (t: [number, number]) => t[0] * 1000 + t[1] / 1e6;
    for (const child of children) {
      expect(toMs(child.startTime as [number, number])).toBeGreaterThanOrEqual(toMs(run.startTime as [number, number]));
      expect(toMs(child.endTime as [number, number])).toBeLessThanOrEqual(toMs(run.endTime as [number, number]));
    }
  });

  it("hides prompt, tool args/results, thinking and assistant text by default", async () => {
    await streamFullRun();
    const text = allAttributeText(spans());
    expect(text).not.toContain(PROMPT_MARKER);
    expect(text).not.toContain(SECRET_ARG);
    expect(text).not.toContain(SECRET_RESULT);
    expect(text).not.toContain("I should list");
    expect(text).not.toContain("Let me");
    expect(text).not.toContain("Summarized the folder");
    for (const span of spans()) {
      expect(span.attributes[S.INPUT_VALUE]).toBeUndefined();
      expect(span.attributes[S.OUTPUT_VALUE]).toBeUndefined();
    }
    expect(resolveContentPolicy()).toEqual({ hideInputs: true, hideOutputs: true });
  });

  it("exports content when traceConfig opts in", async () => {
    await streamFullRun({ hideInputs: false, hideOutputs: false });
    const run = one(spans(), SpanNames.RUN);
    expect(attr(run, S.INPUT_VALUE)).toBe(`List files ${PROMPT_MARKER}`);
    expect(attr(run, S.OUTPUT_VALUE)).toBe(`README says ${SECRET_RESULT}`);
    const ls = one(spans(), `${SpanNames.TOOL_PREFIX}ls`);
    expect(JSON.parse(attr(ls, S.INPUT_VALUE) as string)).toEqual({ path: SECRET_ARG, depth: 1 });
    expect(attr(ls, S.OUTPUT_VALUE)).toContain(SECRET_RESULT);
    expect(attr(one(spans(), SpanNames.THINKING), S.OUTPUT_VALUE)).toBe(THINKING_TEXT);
    expect(byName(spans(), SpanNames.ASSISTANT_MESSAGE).map((m) => attr(m, S.OUTPUT_VALUE))).toEqual([
      "Let me look.",
      `README says ${SECRET_RESULT}`,
    ]);
    // A failing tool's status message can carry tool output: only with outputs visible.
    expect(one(spans(), `${SpanNames.TOOL_PREFIX}read`).status.message).toContain("ENOENT");
  });

  it("opts in through FI_HIDE_INPUTS=false / FI_HIDE_OUTPUTS=false, fail-closed on anything else", async () => {
    process.env.FI_HIDE_INPUTS = " FALSE ";
    process.env.FI_HIDE_OUTPUTS = "no";
    expect(resolveContentPolicy()).toEqual({ hideInputs: false, hideOutputs: true });
    await streamFullRun();
    const run = one(spans(), SpanNames.RUN);
    expect(attr(run, S.INPUT_VALUE)).toBe(`List files ${PROMPT_MARKER}`);
    expect(attr(run, S.OUTPUT_VALUE)).toBeUndefined();
    expect(allAttributeText(spans())).not.toContain(SECRET_RESULT);
  });

  it("never records the API key", async () => {
    await streamFullRun({ hideInputs: false, hideOutputs: false });
    expect(allAttributeText(spans())).not.toContain("crsr_unit_key");
  });

  it("yields the SDK's own event objects and returns the SDK's own agent and run", async () => {
    const steps = fullRun();
    const expected = steps.filter((s): s is { event: never } => "event" in s).map((s) => s.event);
    const { sdk } = instrument({ runs: [steps] });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    expect(agent.constructor.name).toBe("FakeAgent");
    const run = await agent.send("hi");
    expect(run).toBeInstanceOf(FakeRun);
    const seen = await drain(run.stream());
    expect(seen).toHaveLength(expected.length);
    seen.forEach((event, i) => expect(event).toBe(expected[i]));
  });
});

describe("live-recorded run (@cursor/sdk 1.0.38, 2026-10-20)", () => {
  async function streamLive(steps: Step[], traceConfig?: { hideInputs?: boolean; hideOutputs?: boolean }) {
    const { sdk } = instrument({ runs: [steps] }, { traceConfig });
    const agent = await sdk.Agent.create({ model: { id: "composer-2.5", params: [{ id: "fast", value: "false" }] }, local: { cwd: "/tmp" }, agentId: AGENT_ID });
    const run = await agent.send(`Read README.md ${PROMPT_MARKER}`);
    await drain(run.stream());
    const result = await run.wait();
    await until(() => runSpans().length === 1);
    return result;
  }

  it("maps the real event sequence: thinking blocks, read and edit tools, one answer, request id and tokens", async () => {
    await streamLive(liveRun());
    const run = one(spans(), SpanNames.RUN);
    expect(run.status.code).toBe(SpanStatusCode.OK);
    // No `request` event in a live run: the id comes from RunResult.requestId.
    expect(attr(run, C.REQUEST_ID)).toBe("req-1");
    expect(childrenOf(run).map((s) => s.name)).toEqual([
      SpanNames.THINKING,
      `${SpanNames.TOOL_PREFIX}read`,
      SpanNames.THINKING,
      `${SpanNames.TOOL_PREFIX}edit`,
      SpanNames.THINKING,
      SpanNames.ASSISTANT_MESSAGE,
    ]);
    expect(byName(spans(), SpanNames.THINKING).map((s) => attr(s, C.THINKING_DURATION_MS))).toEqual([62, 112, 235]);
    const tools = toolSpans();
    expect(tools.map((s) => attr(s, C.TOOL_CALL_ID))).toEqual([LIVE_READ_ID, LIVE_EDIT_ID]);
    expect(tools.every((s) => s.status.code === SpanStatusCode.OK && attr(s, C.TOOL_STATUS) === "completed")).toBe(true);
    expect(attr(one(spans(), SpanNames.ASSISTANT_MESSAGE), C.TEXT_LENGTH)).toBe(`Created notes.txt with ${SECRET_RESULT}`.length);

    // Cursor's inputTokens excludes cache reads: 23820 + 19172 + 258 = 43250.
    expect(attr(run, S.INPUT_TOKENS)).toBe(42992);
    expect(attr(run, S.OUTPUT_TOKENS)).toBe(258);
    expect(attr(run, S.TOTAL_TOKENS)).toBe(LIVE_USAGE.totalTokens);
    expect(attr(run, S.CACHE_READ_TOKENS)).toBe(19172);
    expect(attr(run, C.USAGE_INPUT_TOKENS)).toBe(LIVE_USAGE.inputTokens);
    expect(attr(run, S.REASONING_TOKENS)).toBeUndefined();

    const text = exportedText(spans());
    for (const secret of [PROMPT_MARKER, SECRET_ARG, SECRET_RESULT, "Reading", "notes.txt"]) expect(text).not.toContain(secret);
  });

  it("puts the real tool args, results, thinking and answer on the spans with content on", async () => {
    await streamLive(liveRun(), { hideInputs: false, hideOutputs: false });
    const run = one(spans(), SpanNames.RUN);
    expect(attr(run, S.INPUT_VALUE)).toContain(PROMPT_MARKER);
    expect(attr(run, S.OUTPUT_VALUE)).toBe(`Created notes.txt with ${SECRET_RESULT}`);
    const [read, edit] = toolSpans();
    expect(attr(read, S.INPUT_VALUE)).toContain(SECRET_ARG);
    expect(attr(read, S.OUTPUT_VALUE)).toContain(SECRET_RESULT);
    expect(attr(edit, S.OUTPUT_VALUE)).toContain(SECRET_RESULT);
    expect(byName(spans(), SpanNames.THINKING).map((s) => attr(s, S.OUTPUT_VALUE))).toEqual(["Reading README.md", "The second line", "Created notes.txt"]);
  });

  it("keeps parallel tool calls apart by call id; a non-zero shell exit is a completed call", async () => {
    await streamLive(liveParallelToolsRun());
    const shell = one(spans(), `${SpanNames.TOOL_PREFIX}shell`);
    const read = one(spans(), `${SpanNames.TOOL_PREFIX}read`);
    expect(attr(shell, C.TOOL_CALL_ID)).toBe("tool_shell");
    expect(attr(read, C.TOOL_CALL_ID)).toBe("tool_read");
    expect(shell.status.code).toBe(SpanStatusCode.OK);
    expect(read.status.code).toBe(SpanStatusCode.OK);
    const ms = (t: [number, number]) => t[0] * 1e3 + t[1] / 1e6;
    // shell opened before read and closed after it.
    expect(ms(shell.startTime)).toBeLessThanOrEqual(ms(read.startTime));
    expect(ms(shell.endTime)).toBeGreaterThan(ms(read.endTime));
    expect(exportedText(spans())).not.toContain(SECRET_RESULT);
  });

  it("reports a run the plan rejects as RunError without a code, and Cursor's text only with content on", async () => {
    const result = await streamLive(liveModelUnavailableRun());
    expect(result.status).toBe("error");
    const run = one(spans(), SpanNames.RUN);
    expect(run.status.code).toBe(SpanStatusCode.ERROR);
    expect(run.status.message).toBe("RunError");
    expect(attr(run, C.RUN_ERROR_CODE)).toBeUndefined();
    expect(exportedText(spans())).not.toContain("Model unavailable");

    exporter.reset();
    await streamLive(liveModelUnavailableRun(), { hideInputs: false, hideOutputs: false });
    expect(exportedText(spans())).toContain(PLAN_ERROR);
  });
});

describe("run outcomes", () => {
  it("marks a failed run ERROR with Cursor's message and code; open tools fail with it", async () => {
    const { sdk } = instrument({ runs: [failedRun()] });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    const run = await agent.send("hi");
    await drain(run.stream());
    const result = await run.wait();
    expect(result.status).toBe("error");
    await until(() => runSpans().length === 1);
    const span = one(spans(), SpanNames.RUN);
    expect(span.status.code).toBe(SpanStatusCode.ERROR);
    // Cursor's run-error text is content: a fixed summary by default.
    expect(span.status.message).toBe("RunError (provider_error)");
    expect(attr(span, C.ERROR_MESSAGE)).toBe("RunError (provider_error)");
    expect(exportedText(spans())).not.toContain("Model provider returned 500");
    expect(attr(span, C.RUN_ERROR_CODE)).toBe("provider_error");
    expect(attr(span, C.RUN_STATUS)).toBe("error");
    const ls = one(spans(), `${SpanNames.TOOL_PREFIX}ls`);
    expect(ls.status.code).toBe(SpanStatusCode.ERROR);
    expect(attr(ls, C.TOOL_INCOMPLETE)).toBe(true);
  });

  it("marks a cancelled run ERROR (cancelled)", async () => {
    const { sdk } = instrument({ runs: [[{ event: events().running }, { finish: "cancelled", delayMs: 2 }]] });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    const run = await agent.send("hi");
    await run.wait();
    await until(() => runSpans().length === 1);
    const span = one(spans(), SpanNames.RUN);
    expect(span.status).toEqual({ code: SpanStatusCode.ERROR, message: "cancelled" });
    expect(attr(span, C.RUN_CANCELLED)).toBe(true);
  });

  it("records a stream that throws as ERROR with the exception, and the caller sees the same error", async () => {
    const boom = new Error("connection reset");
    const { sdk } = instrument({ runs: [[{ event: events().lsStart }, { throw: boom, delayMs: 2 }]] });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    const run = await agent.send("hi");
    await expect(drain(run.stream())).rejects.toBe(boom);
    const span = one(spans(), SpanNames.RUN);
    expect(span.status).toEqual({ code: SpanStatusCode.ERROR, message: "Error" });
    expect(span.events.find((e) => e.name === "exception")?.attributes).toEqual({ "exception.type": "Error" });
    const ls = one(spans(), `${SpanNames.TOOL_PREFIX}ls`);
    expect(ls.status.code).toBe(SpanStatusCode.ERROR);
    expect(attr(ls, C.TOOL_INCOMPLETE)).toBe(true);
  });

  it("records send() rejecting (AuthenticationError) as an ERROR run span, error unchanged", async () => {
    const auth = new FakeAuthenticationError("Invalid User API Key", "agent.send");
    const { sdk } = instrument({ runs: [], sendError: auth });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    await expect(agent.send("hi")).rejects.toBe(auth);
    const span = one(spans(), SpanNames.RUN);
    expect(span.status).toEqual({ code: SpanStatusCode.ERROR, message: "AuthenticationError (401)" });
    expect(attr(span, C.ERROR_MESSAGE)).toBe("AuthenticationError (401)");
    expect(attr(span, C.ERROR_NAME)).toBe("AuthenticationError");
    expect(attr(span, C.ERROR_CODE)).toBe("error");
    expect(attr(span, C.ERROR_STATUS)).toBe(401);
    expect(attr(span, C.ERROR_RETRYABLE)).toBe(false);
    expect(attr(span, C.ERROR_OPERATION)).toBe("agent.send");
    const exception = span.events.find((e) => e.name === "exception");
    expect(exception?.attributes).toEqual({ "exception.type": "AuthenticationError" });
  });

  it("records Agent.create() rejecting as one ERROR 'Cursor Agent create' span, error unchanged", async () => {
    const auth = new FakeAuthenticationError("Invalid User API Key", "Agent.create");
    const { sdk } = instrument({ runs: [] }, { createError: auth });
    await expect(sdk.Agent.create({ apiKey: "crsr_bad", model: { id: "auto" }, local: { cwd: "/tmp" } })).rejects.toBe(auth);
    const span = one(spans(), SpanNames.CREATE);
    expect(span.status.code).toBe(SpanStatusCode.ERROR);
    expect(attr(span, C.RUNTIME)).toBe("local");
    expect(attr(span, S.MODEL_NAME)).toBe("auto");
    expect(allAttributeText(spans())).not.toContain("crsr_bad");
    expect(spans()).toHaveLength(1);
  });

  it("records Agent.resume() rejecting as 'Cursor Agent resume' with the agent id as session", async () => {
    const err = new Error("Agent agent-x not found");
    const { sdk } = instrument({ runs: [] }, { createError: err });
    await expect(sdk.Agent.resume("bc-1234")).rejects.toBe(err);
    const span = one(spans(), SpanNames.RESUME);
    expect(attr(span, C.AGENT_ID)).toBe("bc-1234");
    expect(attr(span, S.SESSION_ID)).toBe("bc-1234");
    expect(attr(span, C.RUNTIME)).toBe("cloud");
  });

  it("records wait() rejecting, and the caller sees the same error", async () => {
    const err = new Error("wait unsupported");
    const { sdk } = instrument({ runs: [[{ event: events().running }]] });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    const original = FakeRun.prototype.wait;
    FakeRun.prototype.wait = () => Promise.reject(err);
    let run: FakeRun;
    try {
      run = await agent.send("hi");
    } finally {
      FakeRun.prototype.wait = original;
    }
    await expect(run.wait()).rejects.toBe(err);
    const span = one(spans(), SpanNames.RUN);
    expect(span.status).toEqual({ code: SpanStatusCode.ERROR, message: "Error" });
    expect(span.events.some((e) => e.name === "exception")).toBe(true);
  });
});

describe("error text is content", () => {
  const PATH = "/Users/alice/secret-proj/.env";
  const MARKER = "ERRMARKER-5521";

  function pathError(): FakeAuthenticationError {
    const error = new FakeAuthenticationError(`ENOENT: open '${PATH}' while handling ${MARKER}`, "agent.send");
    error.stack = `AuthenticationError: ENOENT\n    at send (file://${PATH}/../node_modules/@cursor/sdk/dist/esm/index.js:1:1)`;
    return error;
  }

  async function failEverywhere(traceConfig?: { hideInputs?: boolean; hideOutputs?: boolean }) {
    // 1) Agent.create throws an error carrying a path.
    const create = instrument({ runs: [] }, { createError: pathError(), traceConfig });
    await expect(create.sdk.Agent.create({ model: { id: "auto" } })).rejects.toThrow(MARKER);
    // 2) send() throws one.
    const send = instrument({ runs: [], sendError: pathError() }, { traceConfig });
    const agent = await send.sdk.Agent.create({ model: { id: "auto" } });
    await expect(agent.send("hi")).rejects.toThrow(MARKER);
    // 3) A run fails with a path in Cursor's run error and in the status event.
    const e = events();
    const runFail = instrument(
      {
        runs: [
          [
            { event: e.running },
            { event: { ...e.errored, message: `tool crashed reading ${PATH} ${MARKER}` }, delayMs: 2 },
            { finish: "error", error: { message: `[unknown] ${PATH} ${MARKER}`, code: "internal" } },
          ],
        ],
      },
      { traceConfig },
    );
    const agent2 = await runFail.sdk.Agent.create({ model: { id: "auto" } });
    const run = await agent2.send("hi");
    await drain(run.stream());
    await run.wait();
    await until(() => runSpans().length === 2);
    return exportedText(spans());
  }

  it("default: no message, no stack, no path in events, status or attributes; fixed summaries instead", async () => {
    const text = await failEverywhere();
    expect(text).not.toContain(PATH);
    expect(text).not.toContain("/Users/");
    expect(text).not.toContain(MARKER);
    expect(text).not.toContain("exception.message");
    expect(text).not.toContain("exception.stacktrace");
    const create = one(spans(), SpanNames.CREATE);
    expect(create.status.message).toBe("AuthenticationError (401)");
    expect(create.events[0].attributes).toEqual({ "exception.type": "AuthenticationError" });
    const [sendSpan, failedRun] = runSpans();
    expect(sendSpan.status.message).toBe("AuthenticationError (401)");
    expect(failedRun.status.message).toBe("RunError (internal)");
    expect(attr(failedRun, C.RUN_ERROR_CODE)).toBe("internal");
  });

  it("only hiding one side still redacts error text", async () => {
    const text = await failEverywhere({ hideInputs: false, hideOutputs: true });
    expect(text).not.toContain(PATH);
    expect(text).not.toContain("exception.stacktrace");
  });

  it("opt-in (inputs and outputs visible): full message, stack trace and Cursor's run-error text", async () => {
    const text = await failEverywhere({ hideInputs: false, hideOutputs: false });
    expect(text).toContain(PATH);
    expect(text).toContain("exception.stacktrace");
    expect(text).toContain("exception.message");
    const create = one(spans(), SpanNames.CREATE);
    expect(create.status.message).toContain(MARKER);
    expect(runSpans()[1].status.message).toBe(`[unknown] ${PATH} ${MARKER}`);
  });
});

describe("consumption patterns", () => {
  it("wait() without streaming: run span with RunResult usage and status, no tool spans", async () => {
    const { sdk } = instrument({ runs: [fullRun()] });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    const run = await agent.send("hi");
    const result = await run.wait();
    expect(result.usage).toEqual(USAGE);
    const span = one(spans(), SpanNames.RUN);
    expect(span.status.code).toBe(SpanStatusCode.OK);
    expect(attr(span, S.TOTAL_TOKENS)).toBe(USAGE.totalTokens);
    expect(attr(span, C.USAGE_SOURCE)).toBe("run");
    expect(toolSpans()).toHaveLength(0);
  });

  it("Agent.prompt() is traced through Agent.create -> send -> wait (one run span)", async () => {
    const { sdk } = instrument({ runs: [fullRun()] });
    const result = await sdk.Agent.prompt(`one shot ${PROMPT_MARKER}`, { model: { id: "composer-2" } });
    expect(result.status).toBe("finished");
    expect(spans().map((s) => s.name)).toEqual([SpanNames.RUN]);
    const span = runSpans()[0];
    expect(span.status.code).toBe(SpanStatusCode.OK);
    expect(attr(span, S.MODEL_NAME)).toBe("composer-2");
    expect(attr(span, S.INPUT_TOKENS)).toBe(PROMPT_TOKENS);
  });

  it("early break: open tools end incomplete, the run span ends when the run does", async () => {
    const { sdk } = instrument({ runs: [slowRun()] });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    const run = await agent.send("hi");
    for await (const event of run.stream()) {
      if (event.type === "tool_call") break;
    }
    // The ls tool span ended at the break; the run is still going.
    const ls = one(spans(), `${SpanNames.TOOL_PREFIX}ls`);
    expect(attr(ls, C.TOOL_INCOMPLETE)).toBe(true);
    expect(ls.status.code).toBe(SpanStatusCode.UNSET);
    expect(runSpans()).toHaveLength(0);
    await until(() => runSpans().length === 1);
    const span = runSpans()[0];
    expect(attr(span, C.RUN_STREAM_DETACHED)).toBe(true);
    expect(attr(span, C.RUN_STATUS)).toBe("finished");
    expect(span.status.code).toBe(SpanStatusCode.OK);
    // Tokens come from the handle once the run ends.
    expect(attr(span, S.TOTAL_TOKENS)).toBe(USAGE.totalTokens);
    expect(toolSpans()).toHaveLength(1);
  });

  it("early break on an SDK without status listeners ends the run span at the break", async () => {
    const { sdk } = instrument({ runs: [slowRun()], runOptions: { noStatusListener: true } });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    const run = await agent.send("hi");
    for await (const event of run.stream()) {
      if (event.type === "tool_call") break;
    }
    const span = one(spans(), SpanNames.RUN);
    expect(attr(span, C.RUN_STREAM_DETACHED)).toBe(true);
    expect(attr(span, C.RUN_STATUS)).toBe("running");
    expect(span.status.code).toBe(SpanStatusCode.UNSET);
  });

  it("fire and forget (no stream, no wait): the span ends after the run, within the grace period", async () => {
    const { sdk } = instrument({ runs: [fullRun()] }, { graceMs: 10 });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    await agent.send("hi");
    await until(() => runSpans().length === 1);
    const span = runSpans()[0];
    expect(span.status.code).toBe(SpanStatusCode.OK);
    expect(attr(span, S.TOTAL_TOKENS)).toBe(USAGE.totalTokens);
  });

  it("streaming that starts after the run finished (within the grace period) still gets tool spans", async () => {
    const { sdk } = instrument({ runs: [fullRun()] }, { graceMs: 500 });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    const run = await agent.send("hi");
    await until(() => run.status === "finished");
    await drain(run.stream());
    await until(() => runSpans().length === 1);
    expect(toolSpans()).toHaveLength(3);
  });

  it("stream ends before the handle flips: usage from events, closed by wait() inside the grace period", async () => {
    const { sdk } = instrument({ runs: [fullRun().filter((s) => !("event" in s) || s.event.type !== "status")], runOptions: { statusLagMs: 20, noHandleUsage: true } });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    const run = await agent.send("hi");
    await drain(run.stream());
    expect(runSpans()).toHaveLength(0);
    await run.wait();
    await until(() => runSpans().length === 1);
    const span = runSpans()[0];
    expect(attr(span, C.USAGE_SOURCE)).toBe("events");
    expect(attr(span, S.INPUT_TOKENS)).toBe(PROMPT_TOKENS);
    expect(typeof attr(span, C.RUN_DURATION_MS)).toBe("number");
  });

  it("forceFlush() closes a run waiting in its grace period", async () => {
    const { sdk, instrumentation } = instrument(
      { runs: [fullRun().filter((s) => !("event" in s) || s.event.type !== "status")], runOptions: { statusLagMs: 300 } },
      { graceMs: 60_000 },
    );
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    const run = await agent.send("hi");
    await drain(run.stream());
    expect(runSpans()).toHaveLength(0);
    await instrumentation.forceFlush();
    expect(runSpans()).toHaveLength(1);
    expect(runSpans()[0].status.code).toBe(SpanStatusCode.OK);
  });

  it("shutdown() closes waiting runs of every instrumentation", async () => {
    const { sdk } = instrument(
      { runs: [fullRun().filter((s) => !("event" in s) || s.event.type !== "status")], runOptions: { statusLagMs: 300 } },
      { graceMs: 60_000 },
    );
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    const run = await agent.send("hi");
    await drain(run.stream());
    await shutdown(provider);
    expect(runSpans()).toHaveLength(1);
  });

  it("stream and wait() concurrently: the span waits for the stream so no tool event is lost", async () => {
    const { sdk } = instrument({ runs: [fullRun()] });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    const run = await agent.send("hi");
    const iterator = run.stream();
    const first = await iterator.next();
    expect(first.done).toBe(false);
    const result = await run.wait();
    expect(result.status).toBe("finished");
    expect(runSpans()).toHaveLength(0);
    await drain({ [Symbol.asyncIterator]: () => iterator });
    await until(() => runSpans().length === 1);
    expect(toolSpans()).toHaveLength(3);
    expect(attr(runSpans()[0], C.USAGE_SOURCE)).toBe("run");
  });
});

describe("streams that are never finished", () => {
  it("a partly read stream that is never closed: the span closes after wait() plus the drain timeout", async () => {
    const { sdk } = instrument({ runs: [fullRun()] }, { drainMs: 40 });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    const run = await agent.send("hi");
    const iterator = run.stream();
    await iterator.next();
    await iterator.next();
    const result = await run.wait();
    expect(result.status).toBe("finished");
    expect(runSpans()).toHaveLength(0);
    await until(() => runSpans().length === 1);
    const span = runSpans()[0];
    expect(span.status.code).toBe(SpanStatusCode.OK);
    expect(attr(span, C.RUN_STREAM_DETACHED)).toBe(true);
    expect(attr(span, S.TOTAL_TOKENS)).toBe(USAGE.totalTokens);
  });

  it("forceFlush() closes a partly read stream's run once its end is known", async () => {
    const { sdk, instrumentation } = instrument({ runs: [fullRun()] }, { drainMs: 60_000 });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    const run = await agent.send("hi");
    const iterator = run.stream();
    await iterator.next();
    await run.wait();
    await sleep(20);
    expect(runSpans()).toHaveLength(0);
    await instrumentation.forceFlush();
    expect(runSpans()).toHaveLength(1);
    expect(runSpans()[0].status.code).toBe(SpanStatusCode.OK);
  });

  it("shutdown() closes a run that is still in progress (status UNSET, cursor.run.status=running)", async () => {
    const { sdk } = instrument({ runs: [slowRun()] }, { drainMs: 60_000 });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    const run = await agent.send("hi");
    const iterator = run.stream();
    await iterator.next();
    await shutdown(provider);
    const span = one(spans(), SpanNames.RUN);
    expect(attr(span, C.RUN_STATUS)).toBe("running");
    expect(span.status.code).toBe(SpanStatusCode.UNSET);
    await run.wait();
  });
});

describe("sessions", () => {
  it("two sends on one agent: two traces, one session (the agent id)", async () => {
    const { sdk } = instrument({ runs: [fullRun(AGENT_ID, "run-1"), fullRun(AGENT_ID, "run-2")] });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" }, agentId: AGENT_ID });
    for (const prompt of ["first", "second"]) {
      const run = await agent.send(prompt);
      await drain(run.stream());
    }
    await until(() => runSpans().length === 2);
    const [a, b] = runSpans();
    expect(a.spanContext().traceId).not.toBe(b.spanContext().traceId);
    expect(attr(a, S.SESSION_ID)).toBe(AGENT_ID);
    expect(attr(b, S.SESSION_ID)).toBe(AGENT_ID);
    expect([attr(a, C.RUN_ID), attr(b, C.RUN_ID)].sort()).toEqual(["run-1", "run-2"]);
  });

  it("Agent.resume() agents are traced and keep the resumed id as session", async () => {
    const { sdk } = instrument({ runs: [fullRun("bc-abc", "run-9")] });
    const agent = await sdk.Agent.resume("bc-abc");
    const run = await agent.send("continue");
    await drain(run.stream());
    await until(() => runSpans().length === 1);
    expect(attr(runSpans()[0], S.SESSION_ID)).toBe("bc-abc");
    expect(attr(runSpans()[0], C.RUNTIME)).toBe("cloud");
  });

  it("an app-set fi-core session id wins over the agent id", async () => {
    const manager = new StackContextManager();
    context.setGlobalContextManager(manager);
    try {
      const { sdk } = instrument({ runs: [fullRun()] });
      const agent = await sdk.Agent.create({ model: { id: "composer-2" }, agentId: AGENT_ID });
      const run = await context.with(setSession(context.active(), { sessionId: "app-session-1" }), () => agent.send("hi"));
      await drain(run.stream());
      await until(() => runSpans().length === 1);
      expect(attr(runSpans()[0], S.SESSION_ID)).toBe("app-session-1");
      expect(attr(runSpans()[0], C.AGENT_ID)).toBe(AGENT_ID);
      for (const tool of toolSpans()) expect(attr(tool, S.SESSION_ID)).toBeUndefined();
    } finally {
      context.disable();
    }
  });
});

describe("patching", () => {
  it("is idempotent, reversible, and a disabled instrumentation passes through", async () => {
    const { sdk, instrumentation } = instrument({ runs: [fullRun()] });
    expect(isPatched(sdk.Agent)).toBe(true);
    const patchedCreate = sdk.Agent.create;
    expect(instrumentation.manuallyInstrument(sdk)).toBe(true);
    expect(sdk.Agent.create).toBe(patchedCreate);

    instrumentation.disable();
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    const run = await agent.send("hi");
    await run.wait();
    await sleep(20);
    expect(spans()).toHaveLength(0);

    instrumentation.enable();
    instrumentation.unpatch();
    expect(isPatched(sdk.Agent)).toBe(false);
    expect(Object.prototype.hasOwnProperty.call(sdk.Agent, "create")).toBe(true);
    const plain = await sdk.Agent.create({ model: { id: "composer-2" } });
    expect(isPatched(plain)).toBe(false);
  });

  it("unpatch() also stops agents created before it, without disable()", async () => {
    const { sdk, instrumentation } = instrument({ runs: [fullRun(), fullRun()] });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    instrumentation.unpatch();
    const run = await agent.send("hi");
    await drain(run.stream());
    await run.wait();
    await sleep(20);
    expect(spans()).toHaveLength(0);
    // Instrumenting again resumes tracing for that agent.
    expect(instrumentation.manuallyInstrument(sdk)).toBe(true);
    const again = await agent.send("again");
    await again.wait();
    await until(() => runSpans().length === 1);
  });

  it("wrapAgent() traces an agent obtained elsewhere", async () => {
    const sdk = makeFakeSdk({ runs: [fullRun()] });
    const instrumentation = new CursorSDKInstrumentation({ tracerProvider: provider, runEndGraceMs: 10 });
    instrumentations.push(instrumentation);
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    expect(instrumentation.wrapAgent(agent)).toBe(agent);
    const run = await agent.send("hi");
    await drain(run.stream());
    await until(() => runSpans().length === 1);
    expect(toolSpans()).toHaveLength(3);
  });

  it("returns false for something that is not the SDK", () => {
    const instrumentation = new CursorSDKInstrumentation({ tracerProvider: provider });
    instrumentations.push(instrumentation);
    expect(instrumentation.manuallyInstrument({} as never)).toBe(false);
    expect(instrumentation.manuallyInstrument({ Agent: {} } as never)).toBe(false);
  });
});

describe("robustness", () => {
  it("ignores malformed events and a tool completion repeated after its span ended", async () => {
    const e = events();
    const steps: Step[] = [
      { event: { type: "tool_call" } },
      { event: { type: "assistant", message: "not-an-object" } as never },
      { event: { type: "brand_new_event", foo: 1 } as never },
      { event: e.lsStart },
      { event: e.lsDone, delayMs: 2 },
      { event: e.lsDone },
      { event: { type: "usage", usage: { inputTokens: "x" } } as never },
      { event: e.finished },
      { finish: "finished" },
    ];
    const { sdk } = instrument({ runs: [steps] });
    const agent = await sdk.Agent.create({ model: { id: "composer-2" } });
    const run = await agent.send({ text: "object message", images: [{ url: "x" }] } as never);
    const seen = await drain(run.stream());
    expect(seen).toHaveLength(steps.length - 1);
    await until(() => runSpans().length === 1);
    const span = runSpans()[0];
    expect(span.status.code).toBe(SpanStatusCode.OK);
    expect(attr(span, C.INPUT_IMAGE_COUNT)).toBe(1);
    expect(byName(spans(), `${SpanNames.TOOL_PREFIX}ls`)).toHaveLength(1);
    expect(byName(spans(), `${SpanNames.TOOL_PREFIX}unknown`)).toHaveLength(1);
    expect(attr(span, S.TOTAL_TOKENS)).toBeUndefined();
  });
});
