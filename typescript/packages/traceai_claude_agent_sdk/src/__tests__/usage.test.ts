/**
 * R1 / R2: usage and cost are promoted exactly once.
 *
 * fi-collector promotes gen_ai.usage.*, llm.token_count.*, gen_ai.cost.total and
 * llm.cost.total into hot columns on ANY span, and Observe sums them over every
 * span of a trace and per session.id. So the promoted keys on a conversation
 * span must equal the spend new in that query() call:
 *
 * - tokens come from the latest result's `modelUsage` summed over models; cost
 *   from `total_cost_usd`. Both are running totals for the session (sdk.d.ts:5679,
 *   5687). `result.usage` is main-loop only and per turn (sdk.d.ts:5683).
 * - a resumed, continued or forked session starts from the totals its
 *   transcript saved, so the promoted value is cumulative minus the baseline
 *   this process last saw for that session (the parent session for a fork).
 * - with no known baseline (e.g. after a restart) nothing is promoted; the
 *   cumulative values go on claude_agent.cumulative.* and
 *   claude_agent.usage.baseline_unknown=true.
 */
import type { SDKMessage } from "@anthropic-ai/claude-agent-sdk";
import { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { wrapQuery } from "../index";
import { SessionUsageStore, sessionUsageStore } from "../spans";
import { makeFakeQuery } from "./fixtures/fakeQuery";
import {
  MODEL,
  PROMPT,
  TURN_COST_USD,
  assistant,
  backgroundedSubagentJourney,
  init,
  mcpErrorJourney,
  modelUsage,
  resultError,
  resultSuccess,
  simpleToolJourney,
  streamingInputJourney,
  subagentJourney,
  text,
  turnUsage,
} from "./fixtures/messages";
import { byName, drain, memoryProvider, one } from "./helpers";

const PROMOTED = /^(gen_ai\.usage\.|llm\.token_count\.)|^(gen_ai\.cost\.total|llm\.cost\.total)$/;
const USAGE_KEYS = /^(gen_ai\.usage\.|llm\.token_count\.|claude_agent\.cumulative\.|claude_agent\.cost\.|claude_agent\.usage\.)|^(gen_ai\.cost\.total|llm\.cost\.total)$/;

function promoted(span: ReadableSpan): Record<string, unknown> {
  return Object.fromEntries(Object.entries(span.attributes).filter(([key]) => PROMOTED.test(key)));
}

function usageKeys(span: ReadableSpan): string[] {
  return Object.keys(span.attributes).filter((key) => USAGE_KEYS.test(key));
}

interface Totals {
  cost: number;
  input: number;
  output: number;
  cacheRead?: number;
  cacheCreation?: number;
}

/** One query against session `sessionId` whose result carries running totals `totals`. */
function sessionQuery(sessionId: string, totals: Totals | null, extra: Partial<Parameters<typeof resultSuccess>[1]> = {}) {
  const usageFields = totals
    ? {
        total_cost_usd: totals.cost,
        usage: turnUsage(20, 8),
        modelUsage: {
          [MODEL]: modelUsage(totals.input, totals.output, totals.cost, totals.cacheRead ?? 0, totals.cacheCreation ?? 0),
        },
      }
    : {};
  return [
    init({ session_id: sessionId }),
    assistant("msg_a", [text("Answer.")], null, { session_id: sessionId }),
    resultSuccess("Answer.", { num_turns: 1, session_id: sessionId, ...usageFields, ...extra }),
  ] as SDKMessage[];
}

async function runQuery(messages: SDKMessage[], options: Record<string, unknown> = {}) {
  const { provider, exporter } = memoryProvider();
  await drain(wrapQuery(makeFakeQuery(messages).query, { tracerProvider: provider })({ prompt: PROMPT, options }));
  const spans = exporter.getFinishedSpans();
  return { spans, conversation: one(spans, "claude_agent.conversation") };
}

let counter = 0;
function newSessionId(): string {
  counter += 1;
  return `aaaaaaaa-bbbb-4ccc-8ddd-${counter.toString(16).padStart(12, "0")}`;
}

beforeEach(() => sessionUsageStore().clear());

describe("R2: tokens come from modelUsage, cost from total_cost_usd", () => {
  it("streaming input: the conversation carries the latest running totals, not the last turn's usage", async () => {
    const { conversation } = await runQuery(streamingInputJourney());
    expect(conversation.attributes["gen_ai.usage.input_tokens"]).toBe(40);
    expect(conversation.attributes["gen_ai.usage.output_tokens"]).toBe(16);
    expect(conversation.attributes["gen_ai.usage.total_tokens"]).toBe(56);
    expect(conversation.attributes["gen_ai.cost.total"]).toBeCloseTo(2 * TURN_COST_USD, 12);
    expect(conversation.attributes["claude_agent.cost.total_usd"]).toBeCloseTo(2 * TURN_COST_USD, 12);
    expect(conversation.attributes["claude_agent.cumulative.input_tokens"]).toBe(40);
    expect(conversation.attributes["claude_agent.cumulative.output_tokens"]).toBe(16);
    expect(conversation.attributes["claude_agent.cumulative.cost_usd"]).toBeCloseTo(2 * TURN_COST_USD, 12);
    expect(conversation.attributes["claude_agent.usage.baseline_unknown"]).toBe(false);
  });

  it("subagent journey: sums modelUsage over every model when it exceeds the main-loop usage", async () => {
    const journey = subagentJourney();
    const result = journey[journey.length - 1] as Extract<SDKMessage, { type: "result" }>;
    // usage (main loop only) says 120 in / 45 out; modelUsage adds the subagent's haiku calls.
    result.modelUsage = {
      [MODEL]: modelUsage(120, 45, 0.01, 11, 7),
      "claude-haiku-4-5": modelUsage(300, 80, 0.0023, 5, 3),
    };
    result.total_cost_usd = 0.0123;
    const { conversation } = await runQuery(journey);
    expect(conversation.attributes["gen_ai.usage.input_tokens"]).toBe(420);
    expect(conversation.attributes["gen_ai.usage.output_tokens"]).toBe(125);
    expect(conversation.attributes["gen_ai.usage.total_tokens"]).toBe(545);
    expect(conversation.attributes["gen_ai.usage.cache_read_tokens"]).toBe(16);
    expect(conversation.attributes["gen_ai.usage.cache_creation_tokens"]).toBe(10);
    expect(conversation.attributes["gen_ai.cost.total"]).toBe(0.0123);
  });

  it("turn, tool and subagent spans carry no usage or cost keys", async () => {
    const all: ReadableSpan[] = [];
    for (const journey of [
      simpleToolJourney(),
      subagentJourney(),
      backgroundedSubagentJourney(),
      mcpErrorJourney(),
      streamingInputJourney(),
    ]) {
      all.push(...(await runQuery(journey)).spans);
    }
    const others = all.filter((span) => span.name !== "claude_agent.conversation");
    expect(new Set(others.map((s) => s.attributes["claude_agent.span_kind"]))).toEqual(
      new Set(["assistant_turn", "tool_execution", "mcp_tool", "subagent"]),
    );
    for (const span of others) {
      expect([span.name, usageKeys(span)]).toEqual([span.name, []]);
    }
    for (const conversation of byName(all, "claude_agent.conversation")) {
      expect(Object.keys(promoted(conversation)).length).toBeGreaterThan(0);
    }
  });

  it("a result with no usage or cost leaves every usage and cost attribute out", async () => {
    const journey = simpleToolJourney();
    const result = journey[journey.length - 1] as Record<string, unknown>;
    delete result.usage;
    delete result.modelUsage;
    delete result.total_cost_usd;
    const { spans } = await runQuery(journey);
    for (const span of spans) {
      expect([span.name, usageKeys(span)]).toEqual([span.name, []]);
    }
  });

  it("does not fall back to result.usage when modelUsage is missing", async () => {
    const journey = simpleToolJourney();
    delete (journey[journey.length - 1] as Record<string, unknown>).modelUsage;
    const { conversation } = await runQuery(journey);
    expect(conversation.attributes["gen_ai.usage.input_tokens"]).toBeUndefined();
    expect(conversation.attributes["gen_ai.cost.total"]).toBe(0.0123);
  });
});

describe("R1: resumed, continued and forked sessions are counted once", () => {
  const FIRST: Totals = { cost: TURN_COST_USD, input: 20, output: 8 };
  const BOTH: Totals = { cost: 2 * TURN_COST_USD, input: 40, output: 16 };
  const ALL_THREE: Totals = { cost: 3 * TURN_COST_USD, input: 60, output: 24 };

  function expectPromotedSumsTo(conversations: ReadableSpan[], final: Totals) {
    const sum = (key: string) => conversations.reduce((acc, span) => acc + ((span.attributes[key] as number) ?? 0), 0);
    expect(sum("gen_ai.cost.total")).toBeCloseTo(final.cost, 12);
    expect(sum("gen_ai.usage.input_tokens")).toBe(final.input);
    expect(sum("gen_ai.usage.output_tokens")).toBe(final.output);
    expect(sum("gen_ai.usage.total_tokens")).toBe(final.input + final.output);
  }

  it("resume: promotes cumulative minus the session's last totals (the reviewer's 0.00018 / 0.00036 repro)", async () => {
    const id = newSessionId();
    const q1 = await runQuery(sessionQuery(id, FIRST));
    const q2 = await runQuery(sessionQuery(id, BOTH), { resume: id });
    expect(q2.conversation.attributes["gen_ai.cost.total"]).toBeCloseTo(TURN_COST_USD, 12);
    expect(q2.conversation.attributes["claude_agent.cost.total_usd"]).toBeCloseTo(TURN_COST_USD, 12);
    expect(q2.conversation.attributes["gen_ai.usage.input_tokens"]).toBe(20);
    expect(q2.conversation.attributes["gen_ai.usage.output_tokens"]).toBe(8);
    expect(q2.conversation.attributes["claude_agent.cumulative.cost_usd"]).toBeCloseTo(2 * TURN_COST_USD, 12);
    expect(q2.conversation.attributes["claude_agent.cumulative.input_tokens"]).toBe(40);
    expect(q2.conversation.attributes["claude_agent.usage.baseline_unknown"]).toBe(false);
    expect(q2.conversation.attributes["claude_agent.session.is_new"]).toBe(false);
    expectPromotedSumsTo([q1.conversation, q2.conversation], BOTH);
  });

  it("resume: cache tokens are deltas too", async () => {
    const id = newSessionId();
    await runQuery(sessionQuery(id, { ...FIRST, cacheRead: 100, cacheCreation: 50 }));
    const q2 = await runQuery(sessionQuery(id, { ...BOTH, cacheRead: 250, cacheCreation: 50 }), { resume: id });
    expect(q2.conversation.attributes["gen_ai.usage.cache_read_tokens"]).toBe(150);
    expect(q2.conversation.attributes["gen_ai.usage.cache_creation_tokens"]).toBe(0);
    expect(q2.conversation.attributes["claude_agent.cumulative.cache_read_tokens"]).toBe(250);
    expect(q2.conversation.attributes["claude_agent.cumulative.cache_creation_tokens"]).toBe(50);
  });

  it("continue: handled like resume (same session id from init) and labelled not new", async () => {
    const id = newSessionId();
    const q1 = await runQuery(sessionQuery(id, FIRST));
    const q2 = await runQuery(sessionQuery(id, BOTH), { continue: true });
    expect(q2.conversation.attributes["claude_agent.session.is_new"]).toBe(false);
    expect(q2.conversation.attributes["claude_agent.session.is_resumed"]).toBe(true);
    expect(q2.conversation.attributes["claude_agent.is_resumed"]).toBe(true);
    expect(q2.conversation.attributes["gen_ai.cost.total"]).toBeCloseTo(TURN_COST_USD, 12);
    expectPromotedSumsTo([q1.conversation, q2.conversation], BOTH);
  });

  it("fork: the new session starts from the parent's last totals", async () => {
    const parent = newSessionId();
    const fork = newSessionId();
    const q1 = await runQuery(sessionQuery(parent, FIRST));
    const q2 = await runQuery(sessionQuery(parent, BOTH), { resume: parent });
    const q3 = await runQuery(sessionQuery(fork, ALL_THREE), { resume: parent, forkSession: true });
    expect(q3.conversation.attributes["session.id"]).toBe(fork);
    expect(q3.conversation.attributes["claude_agent.session.fork_from"]).toBe(parent);
    expect(q3.conversation.attributes["gen_ai.cost.total"]).toBeCloseTo(TURN_COST_USD, 12);
    expectPromotedSumsTo([q1.conversation, q2.conversation, q3.conversation], ALL_THREE);
    // The fork's own totals are now its baseline.
    expect(sessionUsageStore().get(fork)?.costUsd).toBeCloseTo(3 * TURN_COST_USD, 12);
  });

  it.each([
    ["resume", (id: string) => ({ resume: id })],
    ["continue", () => ({ continue: true })],
    ["resume + forkSession", (id: string) => ({ resume: id, forkSession: true })],
    ["continue + forkSession", () => ({ continue: true, forkSession: true })],
  ])("%s with no known baseline: no promoted keys, cumulative on claude_agent.cumulative.*", async (_name, opts) => {
    const id = newSessionId(); // never seen in this process, as after a restart
    const { conversation } = await runQuery(sessionQuery(id, BOTH), opts(id));
    expect(promoted(conversation)).toEqual({});
    expect(conversation.attributes["claude_agent.cost.total_usd"]).toBeUndefined();
    expect(conversation.attributes["claude_agent.usage.baseline_unknown"]).toBe(true);
    expect(conversation.attributes["claude_agent.cumulative.cost_usd"]).toBeCloseTo(2 * TURN_COST_USD, 12);
    expect(conversation.attributes["claude_agent.cumulative.input_tokens"]).toBe(40);
    expect(conversation.attributes["claude_agent.cumulative.output_tokens"]).toBe(16);
    expect(conversation.attributes["claude_agent.cumulative.cache_read_tokens"]).toBe(0);
    expect(conversation.attributes["claude_agent.cumulative.cache_creation_tokens"]).toBe(0);
    // The result still seeds the baseline for the next resume in this process.
    const next = await runQuery(sessionQuery(id, ALL_THREE), { resume: id });
    expect(next.conversation.attributes["gen_ai.cost.total"]).toBeCloseTo(TURN_COST_USD, 12);
  });

  it("a new session with Options.sessionId starts from zero even if the id is in the store", async () => {
    const id = newSessionId();
    sessionUsageStore().set(id, { costUsd: 1, inputTokens: 1000, outputTokens: 1000, cacheReadTokens: 0, cacheCreationTokens: 0 });
    const { conversation } = await runQuery(sessionQuery(id, FIRST), { sessionId: id });
    expect(conversation.attributes["gen_ai.cost.total"]).toBeCloseTo(TURN_COST_USD, 12);
    expect(conversation.attributes["gen_ai.usage.input_tokens"]).toBe(20);
  });

  it("a resume whose totals are below the baseline marks the baseline unknown", async () => {
    const id = newSessionId();
    await runQuery(sessionQuery(id, BOTH));
    const { conversation } = await runQuery(sessionQuery(id, FIRST), { resume: id }); // e.g. resumeSessionAt an earlier message
    expect(promoted(conversation)).toEqual({});
    expect(conversation.attributes["claude_agent.usage.baseline_unknown"]).toBe(true);
  });

  it("a mid-query /clear (running total drops) counts the spend before and after it", async () => {
    const id = newSessionId();
    const journey: SDKMessage[] = [
      init({ session_id: id }),
      assistant("msg_c1", [text("one")], null, { session_id: id }),
      resultSuccess("one", { session_id: id, total_cost_usd: 0.0003, modelUsage: { [MODEL]: modelUsage(30, 10, 0.0003) } }),
      assistant("msg_c2", [text("two")], null, { session_id: id }),
      resultSuccess("two", { session_id: id, total_cost_usd: 0.0001, modelUsage: { [MODEL]: modelUsage(10, 4, 0.0001) } }),
    ];
    const { conversation } = await runQuery(journey);
    expect(conversation.attributes["gen_ai.cost.total"]).toBeCloseTo(0.0004, 12);
    expect(conversation.attributes["gen_ai.usage.input_tokens"]).toBe(40);
    expect(conversation.attributes["gen_ai.usage.output_tokens"]).toBe(14);
    expect(conversation.attributes["claude_agent.cumulative.cost_usd"]).toBeCloseTo(0.0001, 12);
  });

  it("a zeroed crash result neither counts nor resets the session baseline", async () => {
    const id = newSessionId();
    const q1 = await runQuery(sessionQuery(id, FIRST));
    const crash: SDKMessage[] = [
      init({ session_id: id }),
      resultError({
        subtype: "error_during_execution",
        session_id: id,
        total_cost_usd: 0,
        modelUsage: {},
        usage: turnUsage(0, 0),
        errors: ["startup failed"],
      }),
    ];
    const q2 = await runQuery(crash, { resume: id });
    expect(usageKeys(q2.conversation)).toEqual([]);
    const q3 = await runQuery(sessionQuery(id, BOTH), { resume: id });
    expect(q3.conversation.attributes["gen_ai.cost.total"]).toBeCloseTo(TURN_COST_USD, 12);
    expectPromotedSumsTo([q1.conversation, q3.conversation], BOTH);
  });

  it("an aborted query with no result promotes nothing", async () => {
    const id = newSessionId();
    const { conversation } = await runQuery(sessionQuery(id, BOTH).slice(0, 2));
    expect(usageKeys(conversation)).toEqual([]);
  });
});

describe("SessionUsageStore", () => {
  it("is a bounded LRU keyed by session id", () => {
    const store = new SessionUsageStore(2);
    store.set("a", { costUsd: 1 });
    store.set("b", { costUsd: 2 });
    expect(store.get("a")?.costUsd).toBe(1); // a is now the most recent
    store.set("c", { costUsd: 3 });
    expect(store.get("b")).toBeUndefined();
    expect(store.get("a")?.costUsd).toBe(1);
    expect(store.get("c")?.costUsd).toBe(3);
    expect(store.size).toBe(2);
  });

  it("the process-wide store keeps at most 1000 sessions", () => {
    const store = sessionUsageStore();
    for (let i = 0; i < 1005; i += 1) store.set(`s-${i}`, { costUsd: i });
    expect(store.size).toBe(1000);
    expect(store.get("s-0")).toBeUndefined();
    expect(store.get("s-1004")?.costUsd).toBe(1004);
  });

  it("stores a copy, so a later change to the caller's object does not move the baseline", () => {
    const store = new SessionUsageStore(10);
    const totals = { costUsd: 1 };
    store.set("x", totals);
    totals.costUsd = 99;
    expect(store.get("x")?.costUsd).toBe(1);
  });
});
