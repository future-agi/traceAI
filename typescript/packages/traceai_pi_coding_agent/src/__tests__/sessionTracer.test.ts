import { SpanStatusCode, context, trace } from "@opentelemetry/api";
import { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { Attr, PiCodingAgentInstrumentation, SpanNames, wrapCreateAgentSession } from "../index";
import { promptTokens } from "../sessionTracer";
import {
  ARG_SECRET,
  FakeSession,
  OUTPUT_SECRET,
  PROMPT_SECRET,
  RESULT_SECRET,
  SESSION_ID,
  SYSTEM_SECRET,
  TOOL_ERROR_SECRET,
  aborted,
  happyPath,
  interrupted,
  nestedTool,
  retryAndToolError,
} from "./fixtures/events";
import { StackContextManager, byName, exportedText, hrToMs, memoryProvider, one, parentId } from "./helpers";

const SECRETS = [PROMPT_SECRET, SYSTEM_SECRET, ARG_SECRET, RESULT_SECRET, OUTPUT_SECRET, TOOL_ERROR_SECRET];

function run(
  events: ReturnType<typeof happyPath>,
  config: ConstructorParameters<typeof PiCodingAgentInstrumentation>[0] = {},
): { spans: ReadableSpan[]; session: FakeSession } {
  const { provider, exporter } = memoryProvider();
  const session = new FakeSession();
  new PiCodingAgentInstrumentation({ tracerProvider: provider, ...config }).instrumentSession(session);
  session.emit(events);
  return { spans: exporter.getFinishedSpans(), session };
}

const CONTENT_ON = { traceConfig: { hideInputs: false, hideOutputs: false } };

describe("span tree", () => {
  it("builds one agent run with turns, LLM calls and tools, all in one trace", () => {
    const { spans } = run(happyPath());
    expect(spans.map((s) => s.name).sort()).toEqual(
      [
        SpanNames.AGENT_RUN,
        "chat gpt-6-luna",
        "chat gpt-6-luna",
        "chat gpt-6-luna",
        "execute_tool ls",
        "execute_tool read",
        "pi.turn",
        "pi.turn",
        "pi.turn",
      ].sort(),
    );
    expect(new Set(spans.map((s) => s.spanContext().traceId)).size).toBe(1);

    const agent = one(spans, SpanNames.AGENT_RUN);
    expect(parentId(agent)).toBeUndefined();
    const turns = byName(spans, "pi.turn");
    for (const turn of turns) expect(parentId(turn)).toBe(agent.spanContext().spanId);
    expect(turns.map((t) => t.attributes[Attr.PI_TURN_INDEX]).sort()).toEqual([0, 1, 2]);

    const ls = one(spans, "execute_tool ls");
    const firstTurn = turns.find((t) => t.attributes[Attr.PI_TURN_INDEX] === 0)!;
    expect(parentId(ls)).toBe(firstTurn.spanContext().spanId);
    for (const llm of byName(spans, "chat gpt-6-luna")) {
      expect(turns.map((t) => t.spanContext().spanId)).toContain(parentId(llm));
    }
  });

  it("sets span kinds the Future AGI collector reads, and session.id on every span", () => {
    const { spans } = run(happyPath());
    const kind = (name: string) => byName(spans, name).map((s) => [s.attributes[Attr.FI_SPAN_KIND], s.attributes[Attr.GEN_AI_SPAN_KIND]]);
    expect(kind(SpanNames.AGENT_RUN)).toEqual([["AGENT", "AGENT"]]);
    expect(new Set(kind("pi.turn").flat())).toEqual(new Set(["CHAIN"]));
    expect(new Set(kind("chat gpt-6-luna").flat())).toEqual(new Set(["LLM"]));
    expect(kind("execute_tool ls")).toEqual([["TOOL", "TOOL"]]);
    for (const span of spans) {
      expect(span.attributes[Attr.SESSION_ID]).toBe(SESSION_ID);
    }
    const agent = one(spans, SpanNames.AGENT_RUN);
    expect(agent.attributes[Attr.GEN_AI_OPERATION_NAME]).toBe("invoke_agent");
    expect(agent.attributes[Attr.GEN_AI_AGENT_NAME]).toBe("pi");
    expect(agent.attributes[Attr.GEN_AI_CONVERSATION_ID]).toBe(SESSION_ID);
    // No model key on non-LLM spans: the collector would otherwise type them LLM.
    for (const span of spans.filter((s) => !s.name.startsWith("chat"))) {
      expect(span.attributes[Attr.REQUEST_MODEL]).toBeUndefined();
    }
  });

  it("records model, provider, tokens, cost and timing on LLM spans", () => {
    const { spans } = run(happyPath());
    const llms = byName(spans, "chat gpt-6-luna").sort((a, b) => hrToMs(a.startTime) - hrToMs(b.startTime));
    const [first, second, last] = llms;
    expect(first.attributes).toMatchObject({
      [Attr.GEN_AI_OPERATION_NAME]: "chat",
      [Attr.REQUEST_MODEL]: "gpt-6-luna",
      [Attr.RESPONSE_MODEL]: "gpt-6-luna",
      [Attr.PROVIDER_NAME]: "cliproxy",
      [Attr.PI_API]: "openai-completions",
      [Attr.INPUT_TOKENS]: 1182,
      [Attr.OUTPUT_TOKENS]: 75,
      [Attr.TOTAL_TOKENS]: 1257,
      [Attr.PI_STOP_REASON]: "toolUse",
      [Attr.PI_TOOL_CALL_COUNT]: 1,
    });
    expect(first.attributes[Attr.PI_TOOL_CALL_NAMES]).toEqual(["ls"]);
    expect(first.attributes[Attr.FINISH_REASONS]).toEqual(["toolUse"]);
    expect(first.attributes[Attr.COST_TOTAL]).toBeUndefined();
    // Pi's usage.input excludes cache reads; the prompt-token count adds them back.
    expect(second.attributes[Attr.INPUT_TOKENS]).toBe(1244);
    expect(second.attributes[Attr.CACHE_READ_TOKENS]).toBe(244);
    expect(second.attributes[Attr.PI_UNCACHED_INPUT_TOKENS]).toBe(1000);
    expect(second.attributes[Attr.COST_TOTAL]).toBe(0.0125);
    expect(last.attributes[Attr.PI_STOP_REASON]).toBe("stop");
    // The LLM span spans the provider request: message.timestamp .. + durationMs.
    expect(hrToMs(first.startTime)).toBe(1_760_000_000_258);
    expect(hrToMs(first.endTime)).toBe(1_760_000_000_258 + 900);
    for (const llm of llms) expect(llm.status.code).toBe(SpanStatusCode.OK);

    const agent = one(spans, SpanNames.AGENT_RUN);
    expect(agent.attributes).toMatchObject({
      [Attr.PI_RUN_LLM_CALL_COUNT]: 3,
      [Attr.PI_RUN_TOOL_CALL_COUNT]: 2,
      [Attr.PI_RUN_TURN_COUNT]: 3,
      [Attr.PI_RUN_ATTEMPTS]: 1,
      [Attr.PI_RUN_INPUT_TOKENS]: 1182 + 1244 + 1325,
      [Attr.PI_RUN_OUTPUT_TOKENS]: 75 + 48 + 35,
      [Attr.PI_RUN_COST_TOTAL]: 0.0125,
    });
    // Run totals are not the promoted gen_ai.usage keys (no double counting).
    expect(agent.attributes[Attr.INPUT_TOKENS]).toBeUndefined();
    expect(agent.status.code).toBe(SpanStatusCode.OK);
  });

  it("records tool identity and timing", () => {
    const { spans } = run(happyPath());
    const ls = one(spans, "execute_tool ls");
    expect(ls.attributes).toMatchObject({
      [Attr.GEN_AI_OPERATION_NAME]: "execute_tool",
      [Attr.GEN_AI_TOOL_NAME]: "ls",
      [Attr.TOOL_NAME]: "ls",
      [Attr.GEN_AI_TOOL_CALL_ID]: "call_ls_1",
      [Attr.TOOL_CALL_ID]: "call_ls_1",
      [Attr.PI_TOOL_DURATION_MS]: 20,
      [Attr.PI_TOOL_IS_ERROR]: false,
    });
    expect(ls.status.code).toBe(SpanStatusCode.OK);
  });

  it("nests a tool called from another tool under the calling tool", () => {
    const { spans } = run(nestedTool());
    const outer = one(spans, "execute_tool codemode");
    const inner = one(spans, "execute_tool read");
    expect(parentId(inner)).toBe(outer.spanContext().spanId);
    expect(inner.attributes[Attr.PI_TOOL_PARENT_CALL_ID]).toBe("outer");
  });

  it("parents the run under the caller's active span when a context manager is registered", () => {
    const manager = new StackContextManager();
    context.setGlobalContextManager(manager);
    try {
      const { provider, exporter } = memoryProvider();
      const session = new FakeSession();
      new PiCodingAgentInstrumentation({ tracerProvider: provider }).instrumentSession(session);
      const appSpan = provider.getTracer("app").startSpan("app.request");
      context.with(trace.setSpan(context.active(), appSpan), () => session.emit(happyPath()));
      appSpan.end();
      const spans = exporter.getFinishedSpans();
      expect(parentId(one(spans, SpanNames.AGENT_RUN))).toBe(appSpan.spanContext().spanId);
      expect(new Set(spans.map((s) => s.spanContext().traceId)).size).toBe(1);
    } finally {
      context.disable();
    }
  });

  it("starts a new trace for each run, linked by session.id", () => {
    const { provider, exporter } = memoryProvider();
    const session = new FakeSession();
    new PiCodingAgentInstrumentation({ tracerProvider: provider }).instrumentSession(session);
    session.emit(happyPath());
    session.emit(happyPath());
    const agents = byName(exporter.getFinishedSpans(), SpanNames.AGENT_RUN);
    expect(agents).toHaveLength(2);
    expect(agents[0].spanContext().traceId).not.toBe(agents[1].spanContext().traceId);
    expect(agents.map((a) => a.attributes[Attr.SESSION_ID])).toEqual([SESSION_ID, SESSION_ID]);
  });
});

describe("errors", () => {
  it("marks a failed provider call ERROR, keeps one run across Pi's retry, and marks a failed tool ERROR", () => {
    const { spans } = run(retryAndToolError());
    const agents = byName(spans, SpanNames.AGENT_RUN);
    expect(agents).toHaveLength(1);
    const agent = agents[0];
    expect(agent.attributes[Attr.PI_RUN_ATTEMPTS]).toBe(2);
    expect(agent.events.map((e) => e.name)).toEqual(["pi.auto_retry.start", "pi.auto_retry.end"]);
    expect(agent.events[0].attributes).toMatchObject({ "pi.attempt": 1, "pi.max_attempts": 3 });
    // The run recovered, so the run itself is OK.
    expect(agent.status.code).toBe(SpanStatusCode.OK);

    const failedLlm = byName(spans, "chat gpt-6-luna").find((s) => s.attributes[Attr.PI_STOP_REASON] === "error")!;
    expect(failedLlm.status).toEqual({ code: SpanStatusCode.ERROR, message: "429 Too Many Requests: rate limited" });
    const failedTurn = byName(spans, "pi.turn").find((s) => s.attributes[Attr.PI_STOP_REASON] === "error")!;
    expect(failedTurn.status.code).toBe(SpanStatusCode.ERROR);

    const tool = one(spans, "execute_tool read");
    expect(tool.status.code).toBe(SpanStatusCode.ERROR);
    expect(tool.attributes[Attr.PI_TOOL_IS_ERROR]).toBe(true);
    // Tool error text is tool output: hidden by default.
    expect(tool.status.message).toBe("tool reported an error (result hidden)");
  });

  it("puts the tool error text in the status only when outputs are on", () => {
    const { spans } = run(retryAndToolError(), CONTENT_ON);
    expect(one(spans, "execute_tool read").status.message).toBe(TOOL_ERROR_SECRET);
  });

  it("marks an aborted run and its model call ERROR", () => {
    const { spans } = run(aborted());
    const agent = one(spans, SpanNames.AGENT_RUN);
    expect(agent.status).toEqual({ code: SpanStatusCode.ERROR, message: "aborted" });
    expect(agent.attributes[Attr.PI_ABORTED]).toBe(true);
    const llm = one(spans, "chat gpt-6-luna");
    expect(llm.status).toEqual({ code: SpanStatusCode.ERROR, message: "Request was aborted" });
    expect(llm.attributes[Attr.PI_ABORTED]).toBe(true);
  });
});

describe("content policy", () => {
  const ENV_KEYS = ["FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS"];
  const saved: Record<string, string | undefined> = {};
  beforeEach(() => {
    for (const key of ENV_KEYS) {
      saved[key] = process.env[key];
      delete process.env[key];
    }
  });
  afterEach(() => {
    for (const key of ENV_KEYS) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
  });

  it("exports no prompt, system prompt, tool argument, tool result or answer text by default", () => {
    const text = exportedText(run(retryAndToolError()).spans) + exportedText(run(happyPath()).spans);
    for (const secret of SECRETS) expect(text).not.toContain(secret);
    expect(text).not.toContain(Attr.INPUT_VALUE);
    expect(text).not.toContain(Attr.OUTPUT_VALUE);
  });

  it("exports prompt, tool arguments, tool results and answers when both are opted in", () => {
    const { spans } = run(happyPath(), CONTENT_ON);
    const agent = one(spans, SpanNames.AGENT_RUN);
    expect(agent.attributes[Attr.INPUT_VALUE]).toBe(PROMPT_SECRET);
    expect(agent.attributes[Attr.OUTPUT_VALUE]).toBe(OUTPUT_SECRET);
    const ls = one(spans, "execute_tool ls");
    expect(JSON.parse(ls.attributes[Attr.INPUT_VALUE] as string)).toEqual({ path: ARG_SECRET });
    expect(ls.attributes[Attr.OUTPUT_VALUE]).toBe(RESULT_SECRET);
    const firstLlm = byName(spans, "chat gpt-6-luna").find((s) => s.attributes[Attr.PI_TOOL_CALL_COUNT] === 1)!;
    expect(firstLlm.attributes[Attr.OUTPUT_VALUE]).toContain("call_ls_1");
    // The system prompt is never exported, even with content on.
    expect(exportedText(spans)).not.toContain(SYSTEM_SECRET);
  });

  it("honours inputs and outputs separately", () => {
    const inputsOnly = exportedText(run(happyPath(), { traceConfig: { hideInputs: false } }).spans);
    expect(inputsOnly).toContain(PROMPT_SECRET);
    expect(inputsOnly).toContain(ARG_SECRET);
    expect(inputsOnly).not.toContain(RESULT_SECRET);
    expect(inputsOnly).not.toContain(OUTPUT_SECRET);

    const outputsOnly = exportedText(run(happyPath(), { traceConfig: { hideOutputs: false } }).spans);
    expect(outputsOnly).not.toContain(PROMPT_SECRET);
    expect(outputsOnly).not.toContain(ARG_SECRET);
    expect(outputsOnly).toContain(RESULT_SECRET);
    expect(outputsOnly).toContain(OUTPUT_SECRET);
  });

  it("opts in through FI_HIDE_INPUTS=false / FI_HIDE_OUTPUTS=false only for an explicit false", () => {
    process.env.FI_HIDE_INPUTS = "1";
    process.env.FI_HIDE_OUTPUTS = "yes";
    expect(exportedText(run(happyPath()).spans)).not.toContain(PROMPT_SECRET);
    process.env.FI_HIDE_INPUTS = " FALSE ";
    process.env.FI_HIDE_OUTPUTS = "false";
    const text = exportedText(run(happyPath()).spans);
    expect(text).toContain(PROMPT_SECRET);
    expect(text).toContain(RESULT_SECRET);
  });

  it("an explicit option beats the environment", () => {
    process.env.FI_HIDE_INPUTS = "false";
    const text = exportedText(run(happyPath(), { traceConfig: { hideInputs: true } }).spans);
    expect(text).not.toContain(PROMPT_SECRET);
  });

  it("truncates long content to maxContentLength", () => {
    const events = happyPath();
    const { spans } = run(events, { ...CONTENT_ON, maxContentLength: 10 });
    const value = one(spans, SpanNames.AGENT_RUN).attributes[Attr.INPUT_VALUE] as string;
    expect(value.startsWith(PROMPT_SECRET.slice(0, 10))).toBe(true);
    expect(value).toContain("[truncated");
  });
});

describe("lifecycle", () => {
  it("ends open spans as incomplete when the session is disposed mid-run", () => {
    const { provider, exporter } = memoryProvider();
    const session = new FakeSession();
    new PiCodingAgentInstrumentation({ tracerProvider: provider }).instrumentSession(session);
    session.emit(interrupted());
    expect(exporter.getFinishedSpans().map((s) => s.name)).not.toContain(SpanNames.AGENT_RUN);
    session.dispose();
    expect(session.disposed).toBe(true);
    const spans = exporter.getFinishedSpans();
    const agent = one(spans, SpanNames.AGENT_RUN);
    expect(agent.attributes[Attr.PI_INCOMPLETE]).toBe(true);
    expect(one(spans, "execute_tool ls").attributes[Attr.PI_INCOMPLETE]).toBe(true);
    expect(one(spans, "pi.turn").attributes[Attr.PI_INCOMPLETE]).toBe(true);
  });

  it("the stop function unsubscribes and is idempotent per session", () => {
    const { provider, exporter } = memoryProvider();
    const session = new FakeSession();
    const instrumentation = new PiCodingAgentInstrumentation({ tracerProvider: provider });
    const stop = instrumentation.instrumentSession(session);
    expect(instrumentation.instrumentSession(session)).toBe(stop);
    expect(session.listenerCount).toBe(1);
    stop();
    stop();
    expect(session.listenerCount).toBe(0);
    session.emit(happyPath());
    expect(exporter.getFinishedSpans()).toHaveLength(0);
  });

  it("never throws into Pi on malformed events", () => {
    const { provider } = memoryProvider();
    const session = new FakeSession();
    new PiCodingAgentInstrumentation({ tracerProvider: provider }).instrumentSession(session);
    const junk = [
      { type: "message_start" },
      { type: "message_end", message: null },
      { type: "tool_execution_end", toolCallId: 7 },
      { type: "turn_end", message: "nope", toolResults: "x" },
      { type: "agent_settled" },
      { nottype: true },
    ] as unknown as ReturnType<typeof happyPath>;
    expect(() => session.emit(junk)).not.toThrow();
  });

  it("wrapCreateAgentSession traces the session it returns and passes arguments through", async () => {
    const { provider, exporter } = memoryProvider();
    const session = new FakeSession("wrapped-session");
    const create = jest.fn(async (options: { cwd: string }) => ({ session, extensionsResult: {}, options }));
    const traced = wrapCreateAgentSession(create, { tracerProvider: provider });
    const result = await traced({ cwd: "/tmp/x" });
    expect(create).toHaveBeenCalledWith({ cwd: "/tmp/x" });
    expect(result.session).toBe(session);
    session.emit(happyPath());
    expect(one(exporter.getFinishedSpans(), SpanNames.AGENT_RUN).attributes[Attr.SESSION_ID]).toBe("wrapped-session");
  });
});

describe("promptTokens", () => {
  it("adds cache reads and writes to Pi's uncached input", () => {
    expect(promptTokens({ input: 10, cacheRead: 5, cacheWrite: 2 })).toBe(17);
    expect(promptTokens({ input: 10 })).toBe(10);
    expect(promptTokens({})).toBeUndefined();
  });
});
