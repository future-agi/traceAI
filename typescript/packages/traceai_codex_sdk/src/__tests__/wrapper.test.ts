import { SpanStatusCode, context, trace } from "@opentelemetry/api";
import { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { TraceConfigOptions, setSession } from "@traceai/fi-core";
import { CodexSDKInstrumentation, instrumentCodexSDK, isPatched } from "../index";
import { usageBaselines } from "../spans";
import {
  ANSWER_MARKER,
  COMMAND_MARKER,
  OUTPUT_MARKER,
  PROMPT,
  PROMPT_MARKER,
  THREAD_ID,
  failedTurn,
  toolTurn,
} from "./fixtures/events";
import { FakeThread, fakeModule, scripted } from "./fixtures/fakeThread";
import { StackContextManager, allAttributeText, byName, drain, memoryProvider, one, parentId } from "./helpers";

function setup(traceConfig?: TraceConfigOptions) {
  const { provider, exporter } = memoryProvider();
  const mod = fakeModule();
  const instrumentation = new CodexSDKInstrumentation({ tracerProvider: provider, traceConfig });
  expect(instrumentation.manuallyInstrument(mod)).toBe(true);
  return { mod, exporter, instrumentation };
}

const ENV_KEYS = ["FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_ERROR_DETAILS"];

beforeEach(() => {
  usageBaselines.clear();
  for (const key of ENV_KEYS) delete process.env[key];
});

describe("runStreamed()", () => {
  it("yields the SDK's events unchanged and records one turn with child item spans", async () => {
    const { mod, exporter } = setup();
    const codex = new mod.Codex([scripted(toolTurn())]);
    const thread = codex.startThread({ model: "gpt-5.6-terra", sandboxMode: "read-only", approvalPolicy: "never" });
    const { events } = await thread.runStreamed(PROMPT);
    const seen = await drain(events);
    expect(seen).toEqual(toolTurn());

    const spans = exporter.getFinishedSpans();
    const turn = one(spans, "codex.turn");
    expect(parentId(turn)).toBeUndefined();
    expect(turn.attributes["fi.span.kind"]).toBe("AGENT");
    expect(turn.attributes["gen_ai.span.kind"]).toBe("AGENT");
    expect(turn.attributes["session.id"]).toBe(THREAD_ID);
    expect(turn.attributes["codex.thread.id"]).toBe(THREAD_ID);
    expect(turn.attributes["gen_ai.conversation.id"]).toBe(THREAD_ID);
    expect(turn.attributes["gen_ai.request.model"]).toBe("gpt-5.6-terra");
    expect(turn.attributes["gen_ai.provider.name"]).toBe("openai");
    expect(turn.attributes["codex.sandbox_mode"]).toBe("read-only");
    expect(turn.attributes["codex.approval_policy"]).toBe("never");
    expect(turn.attributes["codex.thread.resumed"]).toBe(false);
    expect(turn.attributes["codex.input.length"]).toBe(PROMPT.length);
    expect(turn.status.code).toBe(SpanStatusCode.OK);

    const children = spans.filter((s) => s !== turn);
    expect(children.map((s) => s.name)).toEqual([
      "codex.reasoning",
      "codex.agent_message",
      "codex.command_execution",
      "codex.agent_message",
    ]);
    for (const child of children) {
      expect(parentId(child)).toBe(turn.spanContext().spanId);
      expect(child.spanContext().traceId).toBe(turn.spanContext().traceId);
      expect(child.attributes["session.id"]).toBe(THREAD_ID);
    }

    const tool = one(spans, "codex.command_execution");
    expect(tool.attributes["fi.span.kind"]).toBe("TOOL");
    expect(tool.attributes["tool.name"]).toBe("command_execution");
    expect(tool.attributes["gen_ai.tool.call.id"]).toBe("item_2");
    expect(tool.attributes["codex.command.exit_code"]).toBe(0);
    expect(tool.attributes["codex.item.status"]).toBe("completed");
    expect(tool.attributes["codex.item.started_event"]).toBe(true);
    expect(tool.status.code).toBe(SpanStatusCode.OK);

    const [firstMessage] = byName(spans, "codex.agent_message");
    expect(firstMessage.attributes["fi.span.kind"]).toBe("LLM");
    expect(firstMessage.attributes["gen_ai.request.model"]).toBe("gpt-5.6-terra");
    expect(firstMessage.attributes["codex.item.started_event"]).toBe(false);
    expect(one(spans, "codex.reasoning").attributes["fi.span.kind"]).toBe("CHAIN");
  });

  it("puts this turn's tokens on the turn span only", async () => {
    const { mod, exporter } = setup();
    const thread = new mod.Codex([scripted(toolTurn())]).startThread();
    await drain((await thread.runStreamed(PROMPT)).events);
    const spans = exporter.getFinishedSpans();
    const turn = one(spans, "codex.turn");
    expect(turn.attributes["gen_ai.usage.input_tokens"]).toBe(19378);
    expect(turn.attributes["gen_ai.usage.output_tokens"]).toBe(177);
    expect(turn.attributes["gen_ai.usage.total_tokens"]).toBe(19555);
    expect(turn.attributes["gen_ai.usage.cache_read_tokens"]).toBe(16896);
    expect(turn.attributes["gen_ai.usage.cache_write_tokens"]).toBe(0);
    expect(turn.attributes["gen_ai.usage.output_tokens.reasoning"]).toBe(20);
    expect(turn.attributes["codex.cumulative.input_tokens"]).toBe(19378);
    for (const child of spans.filter((s) => s !== turn)) {
      expect(Object.keys(child.attributes).filter((k) => k.startsWith("gen_ai.usage"))).toEqual([]);
    }
  });
});

describe("run()", () => {
  it("returns exactly what the unpatched SDK returns and records the same tree", async () => {
    const plain = new FakeThread([scripted(toolTurn())]);
    const expected = await plain.run(PROMPT);

    const { mod, exporter } = setup();
    const thread = new mod.Codex([scripted(toolTurn())]).startThread();
    const result = await thread.run(PROMPT);
    expect(result).toEqual(expected);
    expect(thread.id).toBe(THREAD_ID);

    const spans = exporter.getFinishedSpans();
    expect(spans.map((s) => s.name).sort()).toEqual(
      ["codex.agent_message", "codex.agent_message", "codex.command_execution", "codex.reasoning", "codex.turn"].sort(),
    );
    expect(one(spans, "codex.turn").status.code).toBe(SpanStatusCode.OK);
  });

  it("marks a failed turn ERROR, closes open items, and rejects with the SDK's error", async () => {
    const { mod, exporter } = setup();
    const thread = new mod.Codex([scripted(failedTurn())]).startThread();
    await expect(thread.run(PROMPT)).rejects.toThrow("stream disconnected before completion");
    const spans = exporter.getFinishedSpans();
    const turn = one(spans, "codex.turn");
    // Error details are independently hidden, even when ordinary output capture is enabled.
    expect(turn.status).toEqual({ code: SpanStatusCode.ERROR });
    expect(turn.attributes["codex.error.message"]).toBeUndefined();
    expect(turn.attributes["codex.cancelled"]).toBeUndefined();
    const tool = one(spans, "codex.command_execution");
    expect(tool.status.code).toBe(SpanStatusCode.ERROR);
    expect(tool.attributes["codex.cancelled"]).toBeUndefined();
  });

  it("records the turn.failed text only with outputs and error details visible", async () => {
    const { mod, exporter } = setup({ hideOutputs: false, hideErrorDetails: false });
    await expect(new mod.Codex([scripted(failedTurn())]).startThread().run(PROMPT)).rejects.toThrow();
    const turn = one(exporter.getFinishedSpans(), "codex.turn");
    expect(turn.status).toEqual({ code: SpanStatusCode.ERROR, message: "stream disconnected before completion" });
  });
});

describe("content policy", () => {
  it("records no prompt, command, output or answer by default", async () => {
    const { mod, exporter } = setup();
    const thread = new mod.Codex([scripted(toolTurn())]).startThread();
    await thread.run(PROMPT);
    const text = allAttributeText(exporter.getFinishedSpans());
    for (const marker of [PROMPT_MARKER, COMMAND_MARKER, OUTPUT_MARKER, ANSWER_MARKER, "Plan: list"]) {
      expect(text).not.toContain(marker);
    }
    for (const span of exporter.getFinishedSpans()) {
      expect(span.attributes["input.value"]).toBeUndefined();
      expect(span.attributes["output.value"]).toBeUndefined();
    }
    // Structural fields stay.
    expect(one(exporter.getFinishedSpans(), "codex.command_execution").attributes["codex.command.output_length"]).toBeGreaterThan(0);
  });

  it("records content when traceConfig opts in", async () => {
    const { mod, exporter } = setup({ hideInputs: false, hideOutputs: false, hideErrorDetails: false });
    const thread = new mod.Codex([scripted(toolTurn())]).startThread();
    await thread.run(PROMPT);
    const spans = exporter.getFinishedSpans();
    const turn = one(spans, "codex.turn");
    expect(turn.attributes["input.value"]).toBe(PROMPT);
    expect(turn.attributes["output.value"]).toContain(ANSWER_MARKER);
    const tool = one(spans, "codex.command_execution");
    expect(tool.attributes["input.value"]).toContain(COMMAND_MARKER);
    expect(tool.attributes["output.value"]).toContain(OUTPUT_MARKER);
  });

  it("opts in only on an explicit FI_HIDE_*=false", async () => {
    for (const value of ["1", "true", " yes ", ""]) {
      process.env.FI_HIDE_INPUTS = value;
      process.env.FI_HIDE_OUTPUTS = value;
      const { mod, exporter } = setup();
      await new mod.Codex([scripted(toolTurn())]).startThread().run(PROMPT);
      expect(allAttributeText(exporter.getFinishedSpans())).not.toContain(PROMPT_MARKER);
    }
    process.env.FI_HIDE_INPUTS = " FALSE ";
    process.env.FI_HIDE_OUTPUTS = "true";
    const { mod, exporter } = setup();
    await new mod.Codex([scripted(toolTurn())]).startThread().run(PROMPT);
    const text = allAttributeText(exporter.getFinishedSpans());
    expect(text).toContain(PROMPT_MARKER);
    expect(text).toContain(COMMAND_MARKER);
    expect(text).not.toContain(OUTPUT_MARKER);
    expect(text).not.toContain(ANSWER_MARKER);
  });
});

describe("token usage across turns", () => {
  const usage = (input: number, output: number, cached = 0) => ({
    input_tokens: input,
    cached_input_tokens: cached,
    cache_write_input_tokens: 0,
    output_tokens: output,
    reasoning_output_tokens: 0,
  });

  it("promotes only the new spend of each turn on one thread (usage is a running total)", async () => {
    const { mod, exporter } = setup();
    const thread = new mod.Codex([
      scripted(toolTurn({ usage: usage(19378, 177, 16896) })),
      scripted(toolTurn({ usage: usage(39318, 314, 34816) })),
    ]).startThread();
    await thread.run("one");
    await thread.run("two");
    const [first, second] = byName(exporter.getFinishedSpans(), "codex.turn");
    expect(first.attributes["gen_ai.usage.input_tokens"]).toBe(19378);
    expect(second.attributes["codex.thread.resumed"]).toBe(true);
    expect(second.attributes["gen_ai.usage.input_tokens"]).toBe(39318 - 19378);
    expect(second.attributes["gen_ai.usage.output_tokens"]).toBe(314 - 177);
    expect(second.attributes["gen_ai.usage.cache_read_tokens"]).toBe(34816 - 16896);
    expect(second.attributes["codex.cumulative.input_tokens"]).toBe(39318);
  });

  it("uses the last total seen in this process when the thread is resumed by id", async () => {
    const { mod, exporter } = setup();
    const codex = new mod.Codex([
      scripted(toolTurn({ usage: usage(100, 10) })),
      scripted(toolTurn({ usage: usage(250, 25) })),
    ]);
    await codex.startThread().run("one");
    await codex.resumeThread(THREAD_ID).run("two");
    const [, resumed] = byName(exporter.getFinishedSpans(), "codex.turn");
    expect(resumed.attributes["gen_ai.usage.input_tokens"]).toBe(150);
    expect(resumed.attributes["codex.usage.baseline_unknown"]).toBeUndefined();
  });

  it("promotes nothing for a resumed thread with no total seen in this process", async () => {
    const { mod, exporter } = setup();
    const codex = new mod.Codex([
      scripted(toolTurn({ usage: usage(49397, 319) })),
      scripted(toolTurn({ usage: usage(59000, 330) })),
    ]);
    const thread = codex.resumeThread(THREAD_ID);
    await thread.run("three");
    await thread.run("four");
    const [first, second] = byName(exporter.getFinishedSpans(), "codex.turn");
    expect(first.attributes["codex.usage.baseline_unknown"]).toBe(true);
    expect(first.attributes["gen_ai.usage.input_tokens"]).toBeUndefined();
    expect(first.attributes["codex.cumulative.input_tokens"]).toBe(49397);
    expect(second.attributes["gen_ai.usage.input_tokens"]).toBe(59000 - 49397);
  });

  it("counts a failed first turn's spend on the next completed turn of a new thread", async () => {
    const { mod, exporter } = setup();
    const thread = new mod.Codex([
      scripted(failedTurn()),
      scripted(toolTurn({ usage: usage(30000, 300) })),
    ]).startThread();
    await expect(thread.run("one")).rejects.toThrow();
    await thread.run("two");
    const [, second] = byName(exporter.getFinishedSpans(), "codex.turn");
    expect(second.attributes["codex.thread.resumed"]).toBe(true);
    expect(second.attributes["codex.usage.baseline_unknown"]).toBeUndefined();
    expect(second.attributes["gen_ai.usage.input_tokens"]).toBe(30000);
  });

  it("counts a cancelled first turn's spend on the next completed turn of a new thread", async () => {
    const { mod, exporter } = setup();
    const thread = new mod.Codex([scripted(toolTurn()), scripted(toolTurn({ usage: usage(41000, 400) }))]).startThread();
    for await (const event of (await thread.runStreamed("one")).events) {
      if (event.type === "item.started") break;
    }
    await thread.run("two");
    const [, second] = byName(exporter.getFinishedSpans(), "codex.turn");
    expect(second.attributes["gen_ai.usage.input_tokens"]).toBe(41000);
  });

  it("uses a total that went down as reported", async () => {
    const { mod, exporter } = setup();
    const thread = new mod.Codex([
      scripted(toolTurn({ usage: usage(500, 50) })),
      scripted(toolTurn({ usage: usage(120, 12) })),
    ]).startThread();
    await thread.run("one");
    await thread.run("two");
    const [, second] = byName(exporter.getFinishedSpans(), "codex.turn");
    expect(second.attributes["gen_ai.usage.input_tokens"]).toBe(120);
    expect(second.attributes["codex.usage.non_monotonic"]).toBe(true);
  });

  it("keeps the larger baseline after a total went down", async () => {
    const { mod, exporter } = setup();
    const thread = new mod.Codex([
      scripted(toolTurn({ usage: usage(500, 50) })),
      scripted(toolTurn({ usage: usage(120, 12) })),
      scripted(toolTurn({ usage: usage(600, 60) })),
    ]).startThread();
    await thread.run("one");
    await thread.run("two");
    await thread.run("three");
    const [, , third] = byName(exporter.getFinishedSpans(), "codex.turn");
    expect(third.attributes["gen_ai.usage.input_tokens"]).toBe(100);
    expect(third.attributes["gen_ai.usage.output_tokens"]).toBe(10);
  });
});

describe("errors and cancellation", () => {
  it("marks the turn ERROR, records the exception, and rethrows the same error when the stream throws", async () => {
    const { mod, exporter } = setup();
    const crash = new Error("Codex Exec exited with code 1: boom");
    const thread = new mod.Codex([scripted(toolTurn().slice(0, 5), { throwAfter: crash })]).startThread();
    const { events } = await thread.runStreamed(PROMPT);
    await expect(drain(events)).rejects.toBe(crash);
    const spans = exporter.getFinishedSpans();
    const turn = one(spans, "codex.turn");
    expect(turn.status).toEqual({ code: SpanStatusCode.ERROR });
    expect(turn.attributes["codex.error.type"]).toBe("Error");
    const exception = turn.events.find((e) => e.name === "exception");
    expect(exception?.attributes).toEqual({ "exception.type": "Error" });
    const tool = one(spans, "codex.command_execution");
    expect(tool.status.code).toBe(SpanStatusCode.ERROR);
  });

  // The SDK throws `Codex Exec exited with code N: <stderr>` and `Failed to parse item: <raw line>`.
  const LEAKY = [
    {
      name: "binary exit with stderr",
      error: new Error(
        `Codex Exec exited with code 1: Error loading config.toml:\n/Users/probeuser/proj/.codex/config.toml:2:9 ${PROMPT_MARKER} at file:///Users/probeuser/app/node_modules/@openai/codex-sdk/dist/index.js:309:15`,
      ),
      summary: "Codex Exec exited with code 1",
    },
    {
      name: "binary killed by a signal",
      error: new Error(`Codex Exec exited with signal SIGKILL: /Users/probeuser ${PROMPT_MARKER}`),
      summary: "Codex Exec exited with signal SIGKILL",
    },
    {
      name: "unparseable stdout line",
      error: new Error(`Failed to parse item: {"aggregated_output":"AWS_SECRET=abc /Users/probeuser ${PROMPT_MARKER}"`),
      summary: "Failed to parse item",
    },
    { name: "other thrown error", error: new TypeError(`bad /Users/probeuser ${PROMPT_MARKER}`), summary: "TypeError" },
  ];

  function everythingExported(spans: ReadableSpan[]): string {
    return JSON.stringify(
      spans.map((span) => ({ attributes: span.attributes, status: span.status, events: span.events.map((e) => ({ name: e.name, attributes: e.attributes })) })),
    );
  }

  it.each(LEAKY)("omits thrown error details by default: $name", async ({ error }) => {
    error.stack = `${error.name}: ${error.message}\n    at CodexExec.run (file:///Users/probeuser/app/node_modules/@openai/codex-sdk/dist/index.js:309:15)`;
    const { mod, exporter } = setup();
    const thread = new mod.Codex([scripted(toolTurn().slice(0, 3), { throwAfter: error })]).startThread();
    await expect(thread.run(PROMPT)).rejects.toBe(error);
    const spans = exporter.getFinishedSpans();
    const turn = one(spans, "codex.turn");
    expect(turn.status).toEqual({ code: SpanStatusCode.ERROR });
    expect(turn.attributes["codex.error.message"]).toBeUndefined();
    const text = everythingExported(spans);
    for (const leak of [PROMPT_MARKER, "/Users/", "node_modules", "AWS_SECRET", "config.toml", "stacktrace"]) {
      expect(text).not.toContain(leak);
    }
  });

  it("records the full error and stack trace only with outputs and error details visible", async () => {
    const { mod, exporter } = setup({ hideOutputs: false, hideErrorDetails: false });
    const error = new Error(`Codex Exec exited with code 1: failed at /Users/probeuser ${PROMPT_MARKER}`);
    await expect(new mod.Codex([scripted(toolTurn().slice(0, 3), { throwAfter: error })]).startThread().run("x")).rejects.toBe(error);
    const turn = one(exporter.getFinishedSpans(), "codex.turn");
    expect(turn.status.message).toContain(PROMPT_MARKER);
    const exception = turn.events.find((e) => e.name === "exception");
    expect(exception?.attributes?.["exception.stacktrace"]).toBeDefined();
  });

  it("marks the turn and open items cancelled when the caller stops reading early", async () => {
    const { mod, exporter } = setup();
    const thread = new mod.Codex([scripted(toolTurn())]).startThread();
    const { events } = await thread.runStreamed(PROMPT);
    for await (const event of events) {
      if (event.type === "item.started") break;
    }
    const spans = exporter.getFinishedSpans();
    const turn = one(spans, "codex.turn");
    expect(turn.status).toEqual({ code: SpanStatusCode.ERROR });
    expect(turn.attributes["codex.cancelled"]).toBe(true);
    const tool = one(spans, "codex.command_execution");
    expect(tool.attributes["codex.cancelled"]).toBe(true);
    expect(tool.status.code).toBe(SpanStatusCode.ERROR);
  });

  it("treats an AbortSignal abort as cancelled, not failed", async () => {
    const { mod, exporter } = setup();
    const controller = new AbortController();
    const thread = new mod.Codex([scripted(toolTurn(), { delayMs: 5 })]).startThread();
    const { events } = await thread.runStreamed(PROMPT, { signal: controller.signal });
    const seen: unknown[] = [];
    await expect(
      (async () => {
        for await (const event of events) {
          seen.push(event);
          if (seen.length === 2) controller.abort();
        }
      })(),
    ).rejects.toMatchObject({ name: "AbortError" });
    const turn = one(exporter.getFinishedSpans(), "codex.turn");
    expect(turn.attributes["codex.cancelled"]).toBe(true);
    expect(turn.status.message).toBeUndefined();
  });

  it("ends the turn OK when the caller stops after turn.completed", async () => {
    const { mod, exporter } = setup();
    const thread = new mod.Codex([scripted(toolTurn())]).startThread();
    const { events } = await thread.runStreamed(PROMPT);
    for await (const event of events) {
      if (event.type === "turn.completed") break;
    }
    expect(one(exporter.getFinishedSpans(), "codex.turn").status.code).toBe(SpanStatusCode.OK);
  });

  it("records an error event with no turn.completed as a failed turn", async () => {
    const { mod, exporter } = setup();
    const thread = new mod.Codex([
      scripted([{ type: "thread.started", thread_id: THREAD_ID }, { type: "error", message: `unauthorized ${PROMPT_MARKER}` }]),
    ]).startThread();
    await drain((await thread.runStreamed(PROMPT)).events);
    const turn = one(exporter.getFinishedSpans(), "codex.turn");
    expect(turn.status).toEqual({ code: SpanStatusCode.ERROR });
    expect(turn.attributes["codex.stream_error.count"]).toBe(1);
    expect(everythingExported(exporter.getFinishedSpans())).not.toContain(PROMPT_MARKER);
  });

  it("ends a turn OK when a retried stream error is followed by turn.completed", async () => {
    const events = toolTurn();
    events.splice(2, 0, { type: "error", message: `Reconnecting... 1/5 (stream disconnected) ${PROMPT_MARKER}` });
    events.splice(3, 0, { type: "error", message: "Reconnecting... 2/5" });
    const { mod, exporter } = setup();
    const thread = new mod.Codex([scripted(events)]).startThread();
    const result = await thread.run(PROMPT);
    expect(result.finalResponse).toContain(ANSWER_MARKER);
    const spans = exporter.getFinishedSpans();
    const turn = one(spans, "codex.turn");
    expect(turn.status.code).toBe(SpanStatusCode.OK);
    expect(turn.attributes["codex.stream_error.count"]).toBe(2);
    expect(turn.attributes["codex.error.message"]).toBeUndefined();
    expect(turn.events.filter((e) => e.name === "codex.stream_error")).toHaveLength(2);
    expect(turn.attributes["gen_ai.usage.input_tokens"]).toBe(19378);
    for (const child of spans.filter((s) => s !== turn)) expect(child.status.code).toBe(SpanStatusCode.OK);
    expect(everythingExported(spans)).not.toContain(PROMPT_MARKER);
  });

  it("keeps the stream error text on its span event only with outputs and error details visible", async () => {
    const events = toolTurn();
    events.splice(2, 0, { type: "error", message: "Reconnecting... 1/5" });
    const { mod, exporter } = setup({ hideOutputs: false, hideErrorDetails: false });
    await new mod.Codex([scripted(events)]).startThread().run(PROMPT);
    const turn = one(exporter.getFinishedSpans(), "codex.turn");
    expect(turn.status.code).toBe(SpanStatusCode.OK);
    expect(turn.events.find((e) => e.name === "codex.stream_error")?.attributes).toEqual({ message: "Reconnecting... 1/5" });
  });
});

describe("item types", () => {
  it("maps file_change, mcp_tool_call, web_search, todo_list, error and unknown items", async () => {
    const events = [
      { type: "thread.started", thread_id: THREAD_ID },
      { type: "turn.started" },
      { type: "item.started", item: { id: "t", type: "todo_list", items: [{ text: "secret step", completed: false }] } },
      {
        type: "item.completed",
        item: { id: "f", type: "file_change", status: "completed", changes: [{ path: "/private/a.ts", kind: "update" }, { path: "/b.ts", kind: "add" }] },
      },
      { type: "item.started", item: { id: "m", type: "mcp_tool_call", server: "docs", tool: "search", arguments: { q: "secret-arg" }, status: "in_progress" } },
      {
        type: "item.completed",
        item: { id: "m", type: "mcp_tool_call", server: "docs", tool: "search", arguments: { q: "secret-arg" }, status: "failed", error: { message: "secret-error" } },
      },
      { type: "item.completed", item: { id: "w", type: "web_search", query: "secret-query" } },
      { type: "item.updated", item: { id: "t", type: "todo_list", items: [{ text: "secret step", completed: true }] } },
      { type: "item.completed", item: { id: "e", type: "error", message: "secret-warning: deprecated key in /Users/probeuser/.codex/config.toml" } },
      { type: "item.completed", item: { id: "x", type: "brand_new_item" } },
      { type: "item.completed", item: { id: "c", type: "command_execution", command: "false", aggregated_output: "", exit_code: 1, status: "failed" } },
      { type: "turn.completed", usage: { input_tokens: 1, output_tokens: 1 } },
    ];
    const { mod, exporter } = setup();
    await drain((await new mod.Codex([scripted(events)]).startThread().runStreamed("go")).events);
    const spans = exporter.getFinishedSpans();

    const file = one(spans, "codex.file_change");
    expect(file.attributes["fi.span.kind"]).toBe("TOOL");
    expect(file.attributes["codex.file_change.count"]).toBe(2);
    expect(file.attributes["codex.file_change.kinds"]).toBe("add,update");

    const mcp = one(spans, "codex.mcp_tool_call");
    expect(mcp.attributes["tool.name"]).toBe("search");
    expect(mcp.attributes["codex.mcp.server"]).toBe("docs");
    expect(mcp.status).toEqual({ code: SpanStatusCode.ERROR });

    expect(one(spans, "codex.web_search").attributes["fi.span.kind"]).toBe("TOOL");

    const todo = one(spans, "codex.todo_list");
    expect(todo.attributes["codex.todo.count"]).toBe(1);
    expect(todo.attributes["codex.todo.completed"]).toBe(1);
    expect(todo.attributes["codex.item.incomplete"]).toBeUndefined();

    // Error items are non-fatal (warnings, deprecations, model reroutes): no ERROR status.
    const error = one(spans, "codex.error");
    expect(error.status.code).toBe(SpanStatusCode.UNSET);
    expect(error.attributes["codex.warning"]).toBe(true);
    expect(error.attributes["codex.error.message"]).toBeUndefined();

    expect(one(spans, "codex.brand_new_item").attributes["fi.span.kind"]).toBe("CHAIN");

    const command = one(spans, "codex.command_execution");
    expect(command.status).toEqual({ code: SpanStatusCode.ERROR });

    expect(one(spans, "codex.turn").status.code).toBe(SpanStatusCode.OK);
    const text = allAttributeText(spans) + JSON.stringify(spans.map((s) => s.status));
    for (const secret of ["/private/a.ts", "secret-arg", "secret-error", "secret-query", "secret step", "secret-warning", "/Users/"]) {
      expect(text).not.toContain(secret);
    }
  });

  it("shows the MCP error text and file paths only with content visible", async () => {
    const events = [
      { type: "thread.started", thread_id: THREAD_ID },
      { type: "item.completed", item: { id: "f", type: "file_change", status: "completed", changes: [{ path: "/private/a.ts", kind: "update" }] } },
      { type: "item.completed", item: { id: "m", type: "mcp_tool_call", server: "docs", tool: "search", arguments: {}, status: "failed", error: { message: "secret-error" } } },
      { type: "item.completed", item: { id: "e", type: "error", message: "secret-warning" } },
      { type: "turn.completed", usage: { input_tokens: 1, output_tokens: 1 } },
    ];
    const { mod, exporter } = setup({ hideInputs: false, hideOutputs: false, hideErrorDetails: false });
    await drain((await new mod.Codex([scripted(events)]).startThread().runStreamed("go")).events);
    const spans = exporter.getFinishedSpans();
    expect(one(spans, "codex.mcp_tool_call").status.message).toBe("secret-error");
    expect(one(spans, "codex.error").attributes["codex.error.message"]).toBe("secret-warning");
    expect(one(spans, "codex.file_change").attributes["input.value"]).toContain("/private/a.ts");
  });
});

describe("patching", () => {
  it("patches the class once and unpatches cleanly", async () => {
    const { provider, exporter } = memoryProvider();
    const mod = fakeModule();
    const a = new CodexSDKInstrumentation({ tracerProvider: provider });
    const b = new CodexSDKInstrumentation({ tracerProvider: provider });
    expect(a.manuallyInstrument(mod)).toBe(true);
    expect(b.manuallyInstrument(mod)).toBe(false);
    expect(isPatched(mod.Thread.prototype)).toBe(true);
    const thread = new mod.Codex([scripted(toolTurn()), scripted(toolTurn())]).startThread();
    expect(a.wrapThread(thread)).toBe(thread);
    await thread.run("one");
    expect(byName(exporter.getFinishedSpans(), "codex.turn")).toHaveLength(1);

    a.unpatch();
    expect(isPatched(mod.Thread.prototype)).toBe(false);
    await thread.run("two");
    expect(byName(exporter.getFinishedSpans(), "codex.turn")).toHaveLength(1);
  });

  it("wrapCodex() traces threads it starts or resumes without touching the class", async () => {
    const { provider, exporter } = memoryProvider();
    const mod = fakeModule();
    const instrumentation = new CodexSDKInstrumentation({ tracerProvider: provider });
    const codex = instrumentation.wrapCodex(new mod.Codex([scripted(toolTurn()), scripted(toolTurn())]));
    expect(isPatched(mod.Thread.prototype)).toBe(false);
    await codex.startThread().run("one");
    await codex.resumeThread(THREAD_ID).run("two");
    expect(byName(exporter.getFinishedSpans(), "codex.turn")).toHaveLength(2);

    const untouched = new mod.Codex([scripted(toolTurn())]);
    await untouched.startThread().run("three");
    expect(byName(exporter.getFinishedSpans(), "codex.turn")).toHaveLength(2);

    instrumentation.unpatch();
    await codex.startThread().run("four").catch(() => undefined);
    expect(byName(exporter.getFinishedSpans(), "codex.turn")).toHaveLength(2);
  });

  it("wrapCodex() keeps no reference per thread it starts", async () => {
    const { provider } = memoryProvider();
    const mod = fakeModule();
    const instrumentation = new CodexSDKInstrumentation({ tracerProvider: provider });
    const codex = instrumentation.wrapCodex(new mod.Codex([]));
    for (let i = 0; i < 5000; i += 1) codex.startThread();
    expect((instrumentation as unknown as { undo: unknown[] }).undo).toHaveLength(2);
  });

  it("a wrapThread() thread passes events through untraced after unpatch()", async () => {
    const { provider, exporter } = memoryProvider();
    const mod = fakeModule();
    const instrumentation = new CodexSDKInstrumentation({ tracerProvider: provider });
    const thread = instrumentation.wrapThread(new mod.Codex([scripted(toolTurn()), scripted(toolTurn())]).startThread());
    await thread.run("one");
    instrumentation.unpatch();
    const result = await thread.run("two");
    expect(result.finalResponse).toContain(ANSWER_MARKER);
    expect(byName(exporter.getFinishedSpans(), "codex.turn")).toHaveLength(1);
  });

  it("instrumentCodexSDK() patches in one call and warns on a module without Thread", () => {
    const mod = fakeModule();
    const instrumentation = instrumentCodexSDK(mod, { tracerProvider: memoryProvider().provider });
    expect(isPatched(mod.Thread.prototype)).toBe(true);
    expect(instrumentation.manuallyInstrument({} as never)).toBe(false);
    instrumentation.unpatch();
  });

  it("falls back to runStreamed() when the private generator is missing", async () => {
    const { provider, exporter } = memoryProvider();
    class PublicOnly {
      async runStreamed() {
        return { events: scripted(toolTurn())("x", {}) };
      }
    }
    const instrumentation = new CodexSDKInstrumentation({ tracerProvider: provider });
    expect(instrumentation.manuallyInstrument({ Thread: PublicOnly as never })).toBe(true);
    const { events } = await new PublicOnly().runStreamed();
    await drain(events);
    expect(byName(exporter.getFinishedSpans(), "codex.turn")).toHaveLength(1);
    instrumentation.unpatch();
  });
});

describe("context", () => {
  it("nests the turn under the caller's active span", async () => {
    const manager = new StackContextManager();
    context.setGlobalContextManager(manager);
    try {
      const { mod, exporter } = setup();
      const { provider } = memoryProvider();
      const parent = provider.getTracer("app").startSpan("app.request");
      const thread = new mod.Codex([scripted(toolTurn())]).startThread();
      await context.with(trace.setSpan(context.active(), parent), () => thread.run("go"));
      parent.end();
      const turn = one(exporter.getFinishedSpans(), "codex.turn");
      expect(parentId(turn)).toBe(parent.spanContext().spanId);
      expect(turn.spanContext().traceId).toBe(parent.spanContext().traceId);
    } finally {
      context.disable();
    }
  });

  it("keeps a session.id the app set through fi-core context", async () => {
    const manager = new StackContextManager();
    context.setGlobalContextManager(manager);
    try {
      const { mod, exporter } = setup();
      const thread = new mod.Codex([scripted(toolTurn())]).startThread();
      await context.with(setSession(context.active(), { sessionId: "app-session" }), () => thread.run("go"));
      const spans = exporter.getFinishedSpans();
      const turn = one(spans, "codex.turn");
      expect(turn.attributes["session.id"]).toBe("app-session");
      expect(turn.attributes["codex.thread.id"]).toBe(THREAD_ID);
      expect(spans.filter((s) => s !== turn).every((s) => s.attributes["session.id"] === undefined)).toBe(true);
    } finally {
      context.disable();
    }
  });
});

describe("tracing never breaks the SDK", () => {
  it("still yields every event when the tracer throws", async () => {
    const mod = fakeModule();
    const broken = {
      getTracer: () => ({
        startSpan: () => {
          throw new Error("tracer down");
        },
        startActiveSpan: () => {
          throw new Error("tracer down");
        },
      }),
    };
    const instrumentation = new CodexSDKInstrumentation({ tracerProvider: broken as never });
    instrumentation.manuallyInstrument(mod);
    const thread = new mod.Codex([scripted(toolTurn())]).startThread();
    const { events } = await thread.runStreamed(PROMPT);
    expect(await drain(events)).toEqual(toolTurn());
  });
});
