/**
 * Every exported item span must have its `codex.turn` parent exported too,
 * however the event stream ends. Each case runs a `run()` turn and then a
 * `runStreamed()` turn on one thread, like the live E2E script.
 *
 * The MCP events use the wire shape of `codex exec --experimental-json`:
 * `McpToolCallItem.result` / `.error` are plain `Option`s in codex-rs
 * (exec/src/exec_events.rs), so they arrive as `null`, not missing, on
 * `item.started` and on a failed call (the SDK's TypeScript types say
 * `result?:`). The same holds for `exit_code: null` in real captures.
 */
import { SpanStatusCode } from "@opentelemetry/api";
import { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { CodexSDKInstrumentation } from "../index";
import { usageBaselines } from "../spans";
import { ThreadEventLike } from "../types";
import { PROMPT, THREAD_ID, toolTurn } from "./fixtures/events";
import { fakeModule, scripted, Script } from "./fixtures/fakeThread";
import { byName, drain, memoryProvider, one, parentId } from "./helpers";

const MCP_ITEM = { id: "item_m", type: "mcp_tool_call", server: "docs", tool: "search", arguments: { q: "codex" } };
const MCP_STARTED: ThreadEventLike = {
  type: "item.started",
  item: { ...MCP_ITEM, result: null, error: null, status: "in_progress" } as never,
};
const MCP_FAILED: ThreadEventLike = {
  type: "item.completed",
  item: { ...MCP_ITEM, result: null, error: { message: "tool call failed" }, status: "failed" } as never,
};
const MCP_COMPLETED: ThreadEventLike = {
  type: "item.completed",
  item: {
    ...MCP_ITEM,
    id: "item_n",
    result: { content: [{ type: "text", text: "ok" }], structured_content: null },
    error: null,
    status: "completed",
  } as never,
};
const MCP_COMPLETED_STARTED: ThreadEventLike = {
  type: "item.started",
  item: { ...MCP_ITEM, id: "item_n", result: null, error: null, status: "in_progress" } as never,
};

function resumed(): ThreadEventLike[] {
  return toolTurn({ usage: { input_tokens: 40000, output_tokens: 300 } });
}

function setup() {
  const { provider, exporter } = memoryProvider();
  const mod = fakeModule();
  new CodexSDKInstrumentation({ tracerProvider: provider }).manuallyInstrument(mod);
  return { mod, exporter };
}

/** Each span's parent is either absent (a root) or another exported span. */
function orphans(spans: ReadableSpan[]): string[] {
  const ids = new Set(spans.map((s) => s.spanContext().spanId));
  return spans.filter((s) => parentId(s) !== undefined && !ids.has(parentId(s) as string)).map((s) => s.name);
}

function endNs(span: ReadableSpan): number {
  return span.endTime[0] * 1e9 + span.endTime[1];
}

type Consume = (events: AsyncGenerator<ThreadEventLike>) => Promise<unknown>;

const readAll: Consume = (events) => drain(events);
const breakOn =
  (type: string): Consume =>
  async (events) => {
    for await (const event of events) if (event.type === type) break;
  };

interface Case {
  name: string;
  second: Script;
  consume: Consume;
  rejects?: boolean;
  status: SpanStatusCode;
}

const CASES: Case[] = [
  { name: "both turns complete", second: scripted(resumed()), consume: readAll, status: SpanStatusCode.OK },
  {
    name: "caller breaks after the first item",
    second: scripted(resumed()),
    consume: breakOn("item.started"),
    status: SpanStatusCode.ERROR,
  },
  {
    name: "stream throws after an item completed",
    second: scripted(resumed().slice(0, 6), { throwAfter: new Error("Codex Exec exited with code 1: boom") }),
    consume: readAll,
    rejects: true,
    status: SpanStatusCode.ERROR,
  },
  {
    name: "turn.failed after an item",
    second: scripted([...resumed().slice(0, 4), { type: "turn.failed", error: { message: "500" } }]),
    consume: readAll,
    status: SpanStatusCode.ERROR,
  },
  {
    name: "stream ends without turn.completed",
    second: scripted(resumed().slice(0, -1)),
    consume: readAll,
    status: SpanStatusCode.OK,
  },
  {
    name: "item events arrive after turn.completed",
    second: scripted([
      ...resumed(),
      { type: "item.completed", item: { id: "late", type: "agent_message", text: "late" } },
    ]),
    consume: readAll,
    status: SpanStatusCode.OK,
  },
  {
    name: "an MCP call (wire shape, result null) is still open when the stream ends",
    second: scripted([...resumed().slice(0, -1), MCP_STARTED]),
    consume: readAll,
    status: SpanStatusCode.OK,
  },
  {
    name: "caller breaks after turn.completed with an MCP call (wire shape) still open",
    second: scripted([...resumed().slice(0, -1), MCP_STARTED, resumed().at(-1) as ThreadEventLike]),
    consume: breakOn("turn.completed"),
    status: SpanStatusCode.OK,
  },
  {
    name: "a failed and a successful MCP call (wire shape)",
    second: scripted([...resumed().slice(0, -1), MCP_STARTED, MCP_FAILED, MCP_COMPLETED_STARTED, MCP_COMPLETED, resumed().at(-1) as ThreadEventLike]),
    consume: readAll,
    status: SpanStatusCode.OK,
  },
];

beforeEach(() => usageBaselines.clear());

describe("every item span keeps its codex.turn root", () => {
  it.each(CASES)("$name", async ({ second, consume, rejects, status }) => {
    const { mod, exporter } = setup();
    const thread = new mod.Codex([scripted(toolTurn()), second]).startThread({ model: "gpt-6-luna" });
    await thread.run(PROMPT);
    const { events } = await thread.runStreamed(PROMPT);
    if (rejects) await expect(consume(events)).rejects.toThrow();
    else await consume(events);

    const spans = exporter.getFinishedSpans();
    const turns = byName(spans, "codex.turn");
    expect(turns).toHaveLength(2);
    expect(orphans(spans)).toEqual([]);
    const streamed = turns[1];
    expect(parentId(streamed)).toBeUndefined();
    expect(streamed.attributes["session.id"]).toBe(THREAD_ID);
    expect(streamed.status.code).toBe(status);
    const children = spans.filter((s) => parentId(s) === streamed.spanContext().spanId);
    expect(children.length).toBeGreaterThan(0);
    for (const child of children) {
      // The turn ends last, so an exporter receives its children first (a backend that
      // inserts in batches can show them a few seconds before the root).
      expect(endNs(child)).toBeLessThanOrEqual(endNs(streamed));
      expect(spans.indexOf(child)).toBeLessThan(spans.indexOf(streamed));
    }
  });

  it("exports a failed MCP call (result null) as an ERROR tool span", async () => {
    const { mod, exporter } = setup();
    const thread = new mod.Codex([scripted([MCP_STARTED, MCP_FAILED, MCP_COMPLETED_STARTED, MCP_COMPLETED])]).startThread();
    await drain((await thread.runStreamed("go")).events);
    const spans = exporter.getFinishedSpans();
    const [failed, ok] = byName(spans, "codex.mcp_tool_call");
    expect(byName(spans, "codex.mcp_tool_call")).toHaveLength(2);
    expect(failed.attributes["codex.item.id"]).toBe("item_m");
    expect(failed.attributes["tool.name"]).toBe("search");
    expect(failed.status).toEqual({ code: SpanStatusCode.ERROR, message: "mcp_tool_call failed" });
    expect(failed.attributes["output.value"]).toBeUndefined();
    expect(ok.attributes["codex.item.id"]).toBe("item_n");
    expect(ok.status.code).toBe(SpanStatusCode.OK);
    expect(parentId(failed)).toBe(one(spans, "codex.turn").spanContext().spanId);
  });

  it("records MCP content (not the text 'null') only with content visible", async () => {
    const { provider, exporter } = memoryProvider();
    const mod = fakeModule();
    new CodexSDKInstrumentation({ tracerProvider: provider, traceConfig: { hideInputs: false, hideOutputs: false } }).manuallyInstrument(mod);
    const thread = new mod.Codex([scripted([MCP_STARTED, MCP_FAILED, MCP_COMPLETED_STARTED, MCP_COMPLETED])]).startThread();
    await drain((await thread.runStreamed("go")).events);
    const [failed, ok] = byName(exporter.getFinishedSpans(), "codex.mcp_tool_call");
    expect(failed.attributes["input.value"]).toBe('{"q":"codex"}');
    expect(failed.attributes["output.value"]).toBeUndefined();
    expect(failed.status.message).toBe("tool call failed");
    expect(ok.attributes["output.value"]).toBe('[{"type":"text","text":"ok"}]');
  });

  it("documented limit: a dropped iterator exports its finished items but never its turn", async () => {
    const { mod, exporter } = setup();
    const thread = new mod.Codex([scripted(toolTurn())]).startThread();
    const { events } = await thread.runStreamed(PROMPT);
    // thread.started, turn.started, reasoning, agent_message; then neither read on nor closed.
    for (let i = 0; i < 4; i += 1) await events.next();
    const spans = exporter.getFinishedSpans();
    expect(byName(spans, "codex.turn")).toHaveLength(0);
    expect(orphans(spans)).toEqual(["codex.reasoning", "codex.agent_message"]);
  });

  it("still ends the turn and every open item when an item's fields cannot be read", async () => {
    const { mod, exporter } = setup();
    const hostile = { id: "item_h", type: "command_execution", command: "ls" } as Record<string, unknown>;
    Object.defineProperty(hostile, "status", {
      enumerable: true,
      get() {
        throw new Error("unreadable");
      },
    });
    const script: Script = async function* () {
      yield { type: "thread.started", thread_id: THREAD_ID };
      yield { type: "item.started", item: { id: "item_o", type: "command_execution", command: "sleep 1", status: "in_progress" } };
      yield { type: "item.started", item: hostile as never };
      yield { type: "item.completed", item: { id: "item_1", type: "agent_message", text: "done" } };
      yield { type: "turn.completed", usage: { input_tokens: 1, output_tokens: 1 } };
    };
    const thread = new mod.Codex([script]).startThread();
    await drain((await thread.runStreamed("go")).events);
    const spans = exporter.getFinishedSpans();
    expect(orphans(spans)).toEqual([]);
    const turn = one(spans, "codex.turn");
    expect(turn.status.code).toBe(SpanStatusCode.OK);
    expect(turn.attributes["gen_ai.usage.input_tokens"]).toBe(1);
    expect(byName(spans, "codex.command_execution")).toHaveLength(2);
    expect(one(spans, "codex.agent_message").status.code).toBe(SpanStatusCode.OK);
  });
});
