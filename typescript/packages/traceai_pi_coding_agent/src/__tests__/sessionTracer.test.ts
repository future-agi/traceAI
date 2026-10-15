import { SpanStatusCode, context, trace } from "@opentelemetry/api";
import { BasicTracerProvider, ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { Attr, PiCodingAgentInstrumentation, SpanNames, wrapCreateAgentSession } from "../index";
import { TOOL_ERROR_HIDDEN, errorDetails, finishReason, promptTokens } from "../sessionTracer";
import {
  ARG_SECRET,
  FakeSession,
  LOCAL_PATH,
  LOCAL_USER,
  LegacySession,
  OUTPUT_SECRET,
  PROMPT_SECRET,
  PROVIDER_ERROR_TEXT,
  RESULT_SECRET,
  SESSION_ID,
  SESSION_NAME_SECRET,
  SUMMARY_SECRET,
  SYSTEM_SECRET,
  TOOL_ERROR_SECRET,
  TOOL_PATH_ERROR,
  aborted,
  cancelledRetry,
  compactionInRun,
  continuedRun,
  happyPath,
  interrupted,
  legacyPrompt,
  legacyRetriesExhausted,
  legacyRetry,
  manualCompaction,
  nestedTool,
  providerAndToolPathErrors,
  retryAndToolError,
} from "./fixtures/events";
import { StackContextManager, byName, exportedText, hrToMs, memoryProvider, one, parentId } from "./helpers";

const SECRETS = [PROMPT_SECRET, SYSTEM_SECRET, ARG_SECRET, RESULT_SECRET, OUTPUT_SECRET, TOOL_ERROR_SECRET];

function run(
  events: ReturnType<typeof happyPath>,
  config: ConstructorParameters<typeof PiCodingAgentInstrumentation>[0] = {},
  session: LegacySession = new FakeSession(),
): { spans: ReadableSpan[]; session: LegacySession } {
  const { provider, exporter } = memoryProvider();
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
        "chat example-model",
        "chat example-model",
        "chat example-model",
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
    for (const llm of byName(spans, "chat example-model")) {
      expect(turns.map((t) => t.spanContext().spanId)).toContain(parentId(llm));
    }
  });

  it("sets span kinds the Future AGI collector reads, and session.id on every span", () => {
    const { spans } = run(happyPath());
    const kind = (name: string) => byName(spans, name).map((s) => [s.attributes[Attr.FI_SPAN_KIND], s.attributes[Attr.GEN_AI_SPAN_KIND]]);
    expect(kind(SpanNames.AGENT_RUN)).toEqual([["AGENT", "AGENT"]]);
    expect(new Set(kind("pi.turn").flat())).toEqual(new Set(["CHAIN"]));
    expect(new Set(kind("chat example-model").flat())).toEqual(new Set(["LLM"]));
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
    const llms = byName(spans, "chat example-model").sort((a, b) => hrToMs(a.startTime) - hrToMs(b.startTime));
    const [first, second, last] = llms;
    expect(first.attributes).toMatchObject({
      [Attr.GEN_AI_OPERATION_NAME]: "chat",
      [Attr.REQUEST_MODEL]: "example-model",
      [Attr.RESPONSE_MODEL]: "example-model",
      [Attr.PROVIDER_NAME]: "my-gateway",
      [Attr.PI_API]: "openai-completions",
      [Attr.INPUT_TOKENS]: 1182,
      [Attr.OUTPUT_TOKENS]: 75,
      [Attr.TOTAL_TOKENS]: 1257,
      [Attr.PI_STOP_REASON]: "toolUse",
      [Attr.PI_TOOL_CALL_COUNT]: 1,
    });
    expect(first.attributes[Attr.PI_TOOL_CALL_NAMES]).toEqual(["ls"]);
    // Pi's toolUse is the OTel tool_calls finish reason; pi.stop_reason keeps Pi's value.
    expect(first.attributes[Attr.FINISH_REASONS]).toEqual(["tool_calls"]);
    expect(first.attributes[Attr.COST_TOTAL]).toBeUndefined();
    // Pi's usage.input excludes cache reads; the prompt-token count adds them back.
    expect(second.attributes[Attr.INPUT_TOKENS]).toBe(1244);
    expect(second.attributes[Attr.CACHE_READ_TOKENS]).toBe(244);
    expect(second.attributes[Attr.PI_UNCACHED_INPUT_TOKENS]).toBe(1000);
    expect(second.attributes[Attr.COST_TOTAL]).toBe(0.0125);
    expect(last.attributes[Attr.PI_STOP_REASON]).toBe("stop");
    expect(last.attributes[Attr.FINISH_REASONS]).toEqual(["stop"]);
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
    expect(agent.attributes[Attr.PI_RUN_RETRY_COUNT]).toBe(1);
    expect(agent.events.map((e) => e.name)).toEqual(["pi.auto_retry.start", "pi.auto_retry.end"]);
    expect(agent.events[0].attributes).toMatchObject({ "pi.attempt": 1, "pi.max_attempts": 3 });
    // The run recovered, so the run itself is OK.
    expect(agent.status.code).toBe(SpanStatusCode.OK);

    const failedLlm = byName(spans, "chat example-model").find((s) => s.attributes[Attr.PI_STOP_REASON] === "error")!;
    // The provider's error text is content: by default only the HTTP status leaves the process.
    expect(failedLlm.status).toEqual({ code: SpanStatusCode.ERROR, message: "error: HTTP 429" });
    expect(failedLlm.attributes[Attr.ERROR_TYPE]).toBe("429");
    const failedTurn = byName(spans, "pi.turn").find((s) => s.attributes[Attr.PI_STOP_REASON] === "error")!;
    expect(failedTurn.status.code).toBe(SpanStatusCode.ERROR);

    const tool = one(spans, "execute_tool read");
    expect(tool.status.code).toBe(SpanStatusCode.ERROR);
    expect(tool.attributes[Attr.PI_TOOL_IS_ERROR]).toBe(true);
    // Tool error text is tool output: hidden by default.
    expect(tool.status.message).toBe(TOOL_ERROR_HIDDEN);
    expect(tool.attributes[Attr.ERROR_TYPE]).toBe("tool_error");
  });

  it("puts the tool and provider error text in the status only when outputs are on", () => {
    const { spans } = run(retryAndToolError(), CONTENT_ON);
    expect(one(spans, "execute_tool read").status.message).toBe(TOOL_ERROR_SECRET);
    const failedLlm = byName(spans, "chat example-model").find((s) => s.attributes[Attr.PI_STOP_REASON] === "error")!;
    expect(failedLlm.status.message).toBe("429 Too Many Requests: rate limited");
  });

  it("keeps local paths and usernames in provider and tool errors out of spans by default", () => {
    const { spans } = run(providerAndToolPathErrors());
    const text = exportedText(spans);
    expect(text).not.toContain(LOCAL_USER);
    expect(text).not.toContain("/Users/");
    expect(text).not.toContain("PROVIDER_ERROR_SECRET");
    expect(text).not.toContain("TOOL_PATH_SECRET");
    // Status: stop reason, HTTP status and the provider's error type only.
    const llm = byName(spans, "chat example-model").find((s) => s.attributes[Attr.PI_STOP_REASON] === "error")!;
    expect(llm.status).toEqual({ code: SpanStatusCode.ERROR, message: "error: HTTP 400 invalid_request_error" });
    expect(llm.attributes[Attr.ERROR_TYPE]).toBe("invalid_request_error");
    const turn = byName(spans, "pi.turn").find((s) => s.attributes[Attr.PI_STOP_REASON] === "error")!;
    expect(turn.status.message).toBe("error: HTTP 400 invalid_request_error");
    const agent = one(spans, SpanNames.AGENT_RUN);
    expect(agent.status).toEqual({ code: SpanStatusCode.ERROR, message: "error: HTTP 400 invalid_request_error" });
    expect(one(spans, "execute_tool read").status).toEqual({ code: SpanStatusCode.ERROR, message: TOOL_ERROR_HIDDEN });
  });

  it("exports the raw provider and tool error text, paths included, only with outputs on", () => {
    const text = exportedText(run(providerAndToolPathErrors(), { traceConfig: { hideOutputs: false } }).spans);
    expect(text).toContain(JSON.stringify(PROVIDER_ERROR_TEXT).slice(1, -1));
    expect(text).toContain(JSON.stringify(TOOL_PATH_ERROR).slice(1, -1));
    // Inputs alone do not reveal error text.
    const inputsOnly = exportedText(run(providerAndToolPathErrors(), { traceConfig: { hideInputs: false } }).spans);
    expect(inputsOnly).not.toContain(LOCAL_PATH);
    expect(inputsOnly).not.toContain(LOCAL_USER);
  });

  it("marks an aborted run and its model call ERROR", () => {
    const { spans } = run(aborted());
    const agent = one(spans, SpanNames.AGENT_RUN);
    expect(agent.status).toEqual({ code: SpanStatusCode.ERROR, message: "aborted" });
    expect(agent.attributes[Attr.PI_ABORTED]).toBe(true);
    const llm = one(spans, "chat example-model");
    expect(llm.status).toEqual({ code: SpanStatusCode.ERROR, message: "aborted" });
    expect(llm.attributes[Attr.PI_ABORTED]).toBe(true);
    expect(llm.attributes[Attr.ERROR_TYPE]).toBe("aborted");
  });
});

describe("errorDetails", () => {
  it("builds the default status from the stop reason, HTTP status and provider error type", () => {
    expect(errorDetails({ stopReason: "error", errorMessage: PROVIDER_ERROR_TEXT }, false)).toEqual({
      type: "invalid_request_error",
      message: "error: HTTP 400 invalid_request_error",
    });
    expect(errorDetails({ stopReason: "error", errorMessage: `Connection refused at ${LOCAL_PATH}` }, false)).toEqual({
      type: "error",
      message: "error",
    });
    expect(
      errorDetails({ stopReason: "error", errorMessage: "fetch failed", diagnostics: [{ type: "x", error: { name: "TypeError" } }] }, false),
    ).toEqual({ type: "TypeError", message: "error: TypeError" });
    expect(errorDetails({ stopReason: "error", errorMessage: "Request failed with status code 503" }, false)).toEqual({
      type: "503",
      message: "error: HTTP 503",
    });
    // An echoed request part's "type" is not an error class.
    expect(
      errorDetails({ stopReason: "error", errorMessage: `502 {"echo":{"type":"text","text":"${LOCAL_PATH}"}}` }, false),
    ).toEqual({ type: "502", message: "error: HTTP 502" });
    expect(
      errorDetails({ stopReason: "error", errorMessage: `429 {"error":{"message":"slow down","type":"tokens"}}` }, false),
    ).toEqual({ type: "tokens", message: "error: HTTP 429 tokens" });
    expect(errorDetails({ stopReason: "aborted", errorMessage: "Request was aborted" }, false)).toEqual({
      type: "aborted",
      message: "aborted",
    });
    expect(errorDetails(undefined, false)).toEqual({ type: "error", message: "error" });
  });

  it("uses the raw text, truncated, when outputs are shown", () => {
    expect(errorDetails({ stopReason: "error", errorMessage: PROVIDER_ERROR_TEXT }, true).message).toBe(PROVIDER_ERROR_TEXT);
    const long = errorDetails({ stopReason: "error", errorMessage: "x".repeat(5000) }, true).message;
    expect(long.startsWith("x".repeat(1000))).toBe(true);
    expect(long).toContain("[truncated 4000 chars]");
  });
});

describe("Pi versions without agent_settled (before 0.80.5)", () => {
  it("ends each run at agent_end: two prompts make two complete traces before dispose", () => {
    const { provider, exporter } = memoryProvider();
    const session = new LegacySession();
    new PiCodingAgentInstrumentation({ tracerProvider: provider }).instrumentSession(session);
    session.emit(legacyPrompt());
    session.emit(legacyPrompt());
    // No dispose: both runs are already exported, complete and OK.
    const spans = exporter.getFinishedSpans();
    const agents = byName(spans, SpanNames.AGENT_RUN);
    expect(agents).toHaveLength(2);
    expect(agents[0].spanContext().traceId).not.toBe(agents[1].spanContext().traceId);
    for (const agent of agents) {
      expect(agent.status.code).toBe(SpanStatusCode.OK);
      expect(agent.attributes[Attr.PI_INCOMPLETE]).toBeUndefined();
      expect(agent.attributes[Attr.PI_RUN_ATTEMPTS]).toBe(1);
      expect(agent.attributes[Attr.PI_RUN_LLM_CALL_COUNT]).toBe(3);
      expect(agent.attributes[Attr.SESSION_ID]).toBe(SESSION_ID);
    }
    // Every child is in its own run's trace.
    for (const span of spans) {
      expect(agents.map((a) => a.spanContext().traceId)).toContain(span.spanContext().traceId);
    }
    expect(spans.filter((s) => s.spanContext().traceId === agents[0].spanContext().traceId)).toHaveLength(9);
    expect(spans.filter((s) => s.spanContext().traceId === agents[1].spanContext().traceId)).toHaveLength(9);
  });

  it("keeps a run open across an agent_end that announces a retry", () => {
    const { spans } = run(legacyRetry(), {}, new LegacySession());
    const agent = one(spans, SpanNames.AGENT_RUN);
    expect(agent.attributes[Attr.PI_RUN_ATTEMPTS]).toBe(2);
    expect(agent.attributes[Attr.PI_RUN_RETRY_COUNT]).toBe(1);
    expect(agent.status.code).toBe(SpanStatusCode.OK);
    expect(new Set(spans.map((s) => s.spanContext().traceId)).size).toBe(1);
  });

  it("ends the run when an automatic retry is cancelled during its backoff, so the next prompt is its own trace", () => {
    const { provider, exporter } = memoryProvider();
    const session = new LegacySession();
    new PiCodingAgentInstrumentation({ tracerProvider: provider }).instrumentSession(session);
    session.emit(cancelledRetry()); // session.abort() / abortRetry() during the retry sleep
    // Ended before the next prompt and without dispose: complete, ERROR with the failed call's status.
    const failed = one(exporter.getFinishedSpans(), SpanNames.AGENT_RUN);
    expect(failed.status).toEqual({ code: SpanStatusCode.ERROR, message: "error: HTTP 503 overloaded_error" });
    expect(failed.attributes[Attr.ERROR_TYPE]).toBe("overloaded_error");
    expect(failed.attributes[Attr.PI_INCOMPLETE]).toBeUndefined();
    expect(failed.attributes[Attr.PI_RUN_ATTEMPTS]).toBe(1);
    expect(failed.attributes[Attr.PI_RUN_RETRY_COUNT]).toBe(1);
    expect(failed.events.map((e) => e.name)).toEqual(["pi.auto_retry.start", "pi.auto_retry.end"]);
    expect(failed.events[1].attributes).toMatchObject({ "pi.attempt": 1, "pi.success": false });

    session.emit(legacyPrompt());
    const spans = exporter.getFinishedSpans();
    const agents = byName(spans, SpanNames.AGENT_RUN);
    expect(agents).toHaveLength(2);
    const [first, second] = agents.map((a) => a.spanContext().traceId);
    expect(second).not.toBe(first);
    expect(agents[1].status.code).toBe(SpanStatusCode.OK);
    expect(agents[1].attributes[Attr.PI_RUN_ATTEMPTS]).toBe(1);
    expect(agents[1].attributes[Attr.PI_RUN_RETRY_COUNT]).toBe(0);
    expect(agents[1].attributes[Attr.PI_RUN_LLM_CALL_COUNT]).toBe(3);
    // The failed run's trace holds only its own run, turn and model call.
    expect(spans.filter((s) => s.spanContext().traceId === first).map((s) => s.name).sort()).toEqual(
      [SpanNames.AGENT_RUN, "chat example-model", "pi.turn"].sort(),
    );
    expect(spans.filter((s) => s.spanContext().traceId === second)).toHaveLength(9);
  });

  it("ends a run whose retries all fail at its last agent_end; the final auto_retry_end changes nothing", () => {
    const { spans } = run(legacyRetriesExhausted(), {}, new LegacySession());
    const agent = one(spans, SpanNames.AGENT_RUN);
    expect(agent.attributes[Attr.PI_RUN_ATTEMPTS]).toBe(2);
    expect(agent.attributes[Attr.PI_RUN_RETRY_COUNT]).toBe(1);
    expect(agent.status).toEqual({ code: SpanStatusCode.ERROR, message: "error: HTTP 503 overloaded_error" });
    expect(spans).toHaveLength(5); // run, 2 turns, 2 model calls
    expect(new Set(spans.map((s) => s.spanContext().traceId)).size).toBe(1);
  });

  it("switches to agent_settled once a session without waitForIdle() is seen to settle", () => {
    const { provider, exporter } = memoryProvider();
    const session = new LegacySession();
    new PiCodingAgentInstrumentation({ tracerProvider: provider }).instrumentSession(session);
    session.emit(happyPath()); // ends at agent_end; agent_settled then marks the session as settling
    session.emit(continuedRun()); // the continuation after agent_end stays in the same run
    const agents = byName(exporter.getFinishedSpans(), SpanNames.AGENT_RUN);
    expect(agents).toHaveLength(2);
    expect(agents[1].attributes[Attr.PI_RUN_ATTEMPTS]).toBe(2);
    expect(agents[1].attributes[Attr.PI_RUN_LLM_CALL_COUNT]).toBe(4);
  });

  it("keeps a continuation after agent_end in the run when the session has waitForIdle() (Pi 0.80.5+)", () => {
    const { spans } = run(continuedRun());
    const agent = one(spans, SpanNames.AGENT_RUN);
    expect(agent.attributes[Attr.PI_RUN_ATTEMPTS]).toBe(2);
    expect(agent.attributes[Attr.PI_RUN_TURN_COUNT]).toBe(4);
    expect(new Set(spans.map((s) => s.spanContext().traceId)).size).toBe(1);
  });

  it("leaves a cancelled retry to agent_settled when the session has waitForIdle() (Pi 0.80.5+)", () => {
    const { provider, exporter } = memoryProvider();
    const session = new FakeSession();
    new PiCodingAgentInstrumentation({ tracerProvider: provider }).instrumentSession(session);
    session.emit(cancelledRetry());
    expect(byName(exporter.getFinishedSpans(), SpanNames.AGENT_RUN)).toHaveLength(0);
    session.emit([{ type: "agent_settled", aborted: true }]);
    const agent = one(exporter.getFinishedSpans(), SpanNames.AGENT_RUN);
    expect(agent.status).toEqual({ code: SpanStatusCode.ERROR, message: "aborted" });
    expect(agent.events.map((e) => e.name)).toEqual(["pi.auto_retry.start", "pi.auto_retry.end"]);
  });
});

describe("compaction", () => {
  it("records compaction inside a run as a CHAIN child with its own token usage", () => {
    const { spans } = run(compactionInRun());
    const agent = one(spans, SpanNames.AGENT_RUN);
    const compaction = one(spans, SpanNames.COMPACTION);
    expect(parentId(compaction)).toBe(agent.spanContext().spanId);
    expect(compaction.attributes).toMatchObject({
      [Attr.FI_SPAN_KIND]: "CHAIN",
      [Attr.GEN_AI_SPAN_KIND]: "CHAIN",
      [Attr.SESSION_ID]: SESSION_ID,
      [Attr.PI_COMPACTION_REASON]: "threshold",
      [Attr.PI_COMPACTION_TOKENS_BEFORE]: 180_000,
      [Attr.PI_COMPACTION_TOKENS_AFTER]: 12_000,
      [Attr.PI_COMPACTION_WILL_RETRY]: false,
      [Attr.INPUT_TOKENS]: 6000,
      [Attr.OUTPUT_TOKENS]: 700,
      [Attr.CACHE_READ_TOKENS]: 1000,
      [Attr.COST_TOTAL]: 0.02,
    });
    expect(compaction.attributes[Attr.REQUEST_MODEL]).toBeUndefined();
    expect(compaction.status.code).toBe(SpanStatusCode.OK);
    // Run totals include the compaction model call.
    expect(agent.attributes[Attr.PI_RUN_INPUT_TOKENS]).toBe(1182 + 1244 + 1325 + 6000);
    expect(agent.attributes[Attr.PI_RUN_COST_TOTAL]).toBeCloseTo(0.0325);
    // The summary is model-written text about the conversation: hidden by default.
    expect(exportedText(spans)).not.toContain(SUMMARY_SECRET);
    expect(one(run(compactionInRun(), CONTENT_ON).spans, SpanNames.COMPACTION).attributes[Attr.OUTPUT_VALUE]).toBe(SUMMARY_SECRET);
  });

  it("records a compaction outside a run (session.compact(), before a prompt) as its own trace", () => {
    const { spans } = run(manualCompaction());
    expect(spans.map((s) => s.name)).toEqual([SpanNames.COMPACTION]);
    const [compaction] = spans;
    expect(parentId(compaction)).toBeUndefined();
    expect(compaction.attributes).toMatchObject({ [Attr.PI_COMPACTION_REASON]: "manual", [Attr.SESSION_ID]: SESSION_ID, [Attr.INPUT_TOKENS]: 800 });
  });

  it("marks a failed compaction ERROR without its error text by default", () => {
    const failure = `summarization failed reading ${LOCAL_PATH}`;
    const hidden = one(run(manualCompaction(failure)).spans, SpanNames.COMPACTION);
    expect(hidden.status).toEqual({ code: SpanStatusCode.ERROR, message: "compaction failed" });
    expect(exportedText([hidden])).not.toContain(LOCAL_USER);
    const shown = one(run(manualCompaction(failure), CONTENT_ON).spans, SpanNames.COMPACTION);
    expect(shown.status.message).toBe(failure);
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

  it("exports the session name (user-authored) only when inputs are shown", () => {
    const named = () => {
      const session = new FakeSession();
      session.sessionName = SESSION_NAME_SECRET;
      return session;
    };
    const hidden = run(happyPath(), {}, named()).spans;
    expect(exportedText(hidden)).not.toContain(SESSION_NAME_SECRET);
    expect(one(hidden, SpanNames.AGENT_RUN).attributes[Attr.PI_SESSION_NAME]).toBeUndefined();
    const shown = run(happyPath(), { traceConfig: { hideInputs: false } }, named()).spans;
    expect(one(shown, SpanNames.AGENT_RUN).attributes[Attr.PI_SESSION_NAME]).toBe(SESSION_NAME_SECRET);
  });

  it("exports prompt, tool arguments, tool results and answers when both are opted in", () => {
    const { spans } = run(happyPath(), CONTENT_ON);
    const agent = one(spans, SpanNames.AGENT_RUN);
    expect(agent.attributes[Attr.INPUT_VALUE]).toBe(PROMPT_SECRET);
    expect(agent.attributes[Attr.OUTPUT_VALUE]).toBe(OUTPUT_SECRET);
    const ls = one(spans, "execute_tool ls");
    expect(JSON.parse(ls.attributes[Attr.INPUT_VALUE] as string)).toEqual({ path: ARG_SECRET });
    expect(ls.attributes[Attr.OUTPUT_VALUE]).toBe(RESULT_SECRET);
    const firstLlm = byName(spans, "chat example-model").find((s) => s.attributes[Attr.PI_TOOL_CALL_COUNT] === 1)!;
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

  it("ends children before their parents and drops a streaming finish reason when disposed mid-call", () => {
    const { provider, exporter } = memoryProvider();
    const session = new FakeSession();
    new PiCodingAgentInstrumentation({ tracerProvider: provider }).instrumentSession(session);
    // Stop while the nested tool runs and while a model call streams in the same turn.
    const events = nestedTool().slice(0, 4);
    events.push({ type: "message_start", message: { role: "assistant", model: "m", stopReason: "pending", content: [] } });
    session.emit(events);
    session.dispose();
    const order = exporter.getFinishedSpans().map((s) => s.name);
    expect(order).toEqual(["execute_tool read", "execute_tool codemode", "chat m", "pi.turn", SpanNames.AGENT_RUN]);
    const llm = one(exporter.getFinishedSpans(), "chat m");
    expect(llm.attributes[Attr.FINISH_REASONS]).toBeUndefined();
    expect(llm.attributes[Attr.PI_STOP_REASON]).toBeUndefined();
    expect(llm.attributes[Attr.PI_INCOMPLETE]).toBe(true);
  });

  it("ends a tool span without a toolCallId at its end event, with the end event's status", () => {
    const { provider, exporter } = memoryProvider();
    const session = new FakeSession();
    new PiCodingAgentInstrumentation({ tracerProvider: provider }).instrumentSession(session);
    session.emit([
      { type: "agent_start" },
      { type: "turn_start" },
      { type: "tool_execution_start", toolName: "bash", args: {} },
      { type: "tool_execution_end", toolName: "bash", result: { content: [] }, isError: true, durationMs: 7 },
    ]);
    const tools = byName(exporter.getFinishedSpans(), "execute_tool bash");
    expect(tools).toHaveLength(1);
    expect(tools[0].status.code).toBe(SpanStatusCode.ERROR);
    expect(tools[0].attributes[Attr.PI_TOOL_IS_ERROR]).toBe(true);
    expect(tools[0].attributes[Attr.PI_TOOL_DURATION_MS]).toBe(7);
    session.dispose();
    expect(byName(exporter.getFinishedSpans(), "execute_tool bash")).toHaveLength(1);
  });

  it("traces the session again after the stop function, and restores dispose()", () => {
    const { provider, exporter } = memoryProvider();
    const session = new FakeSession();
    const instrumentation = new PiCodingAgentInstrumentation({ tracerProvider: provider });
    const originalDispose = session.dispose;
    const stop = instrumentation.instrumentSession(session);
    expect(session.dispose).not.toBe(originalDispose);
    stop();
    expect(session.dispose).toBe(originalDispose);
    expect(Object.prototype.hasOwnProperty.call(session, "dispose")).toBe(false);
    const again = instrumentation.instrumentSession(session);
    expect(again).not.toBe(stop);
    expect(session.listenerCount).toBe(1);
    session.emit(happyPath());
    expect(byName(exporter.getFinishedSpans(), SpanNames.AGENT_RUN)).toHaveLength(1);
  });

  it("still runs Pi's dispose() when ending the spans throws", () => {
    const throwing = {
      onStart: () => {},
      onEnd: () => {
        throw new Error("span processor failed");
      },
      forceFlush: async () => {},
      shutdown: async () => {},
    };
    const badProvider = new BasicTracerProvider({ spanProcessors: [throwing] });
    const session = new FakeSession();
    new PiCodingAgentInstrumentation({ tracerProvider: badProvider }).instrumentSession(session);
    session.emit(interrupted());
    expect(() => session.dispose()).not.toThrow();
    expect(session.disposed).toBe(true);
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

describe("finishReason", () => {
  it("maps Pi stop reasons to OTel finish reasons", () => {
    expect(finishReason("toolUse")).toBe("tool_calls");
    expect(finishReason("stop")).toBe("stop");
    expect(finishReason("length")).toBe("length");
    expect(finishReason("pending")).toBeUndefined();
    expect(finishReason(undefined)).toBeUndefined();
  });
});

describe("promptTokens", () => {
  it("adds cache reads and writes to Pi's uncached input", () => {
    expect(promptTokens({ input: 10, cacheRead: 5, cacheWrite: 2 })).toBe(17);
    expect(promptTokens({ input: 10 })).toBe(10);
    expect(promptTokens({})).toBeUndefined();
  });
});
