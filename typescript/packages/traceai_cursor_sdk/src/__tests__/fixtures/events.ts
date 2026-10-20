/**
 * Stream events in the shapes `@cursor/sdk` 1.0.37 emits (`messages.d.ts`),
 * following the local runtime's mapping in dist/esm/index.js:
 *
 *   user-message-appended -> { type: "user", message: { role, content: [text] } }
 *   text-delta            -> { type: "assistant", message: { role, content: [text chunk] } }
 *   thinking-delta        -> { type: "thinking", text: chunk }
 *   thinking-completed    -> { type: "thinking", text: "", thinking_duration_ms }
 *   tool-call-started / partial-tool-call / tool-call-completed
 *                         -> { type: "tool_call", call_id, name, status, args?, result?, truncated? }
 *   summary               -> { type: "task", text }
 *   turn-ended            -> { type: "usage", usage: TokenUsage }
 *   status changes        -> { type: "status", status: "RUNNING" | "FINISHED" | "ERROR", message? }
 *   request               -> { type: "request", request_id }
 */
import type { SDKMessageLike, TokenUsageLike } from "../../types";
import type { Step } from "./fakeSdk";

export const AGENT_ID = "agent-11111111-2222-4333-8444-555555555555";
export const RUN_ID = "run-1";
export const MODEL = { id: "composer-2", params: [{ id: "thinking", value: "high" }] };
export const PROMPT_MARKER = "MARKER-CURSOR-UNIT-7731";
export const SECRET_ARG = "/home/dev/secret-project/README.md";
export const SECRET_RESULT = "TOP-SECRET-FILE-CONTENTS";
export const THINKING_TEXT = "I should list files first.";

const base = (agentId = AGENT_ID, runId = RUN_ID) => ({ agent_id: agentId, run_id: runId });

// Cursor's totalTokens = input + output + cache read + cache write (inputTokens excludes the cache).
export const USAGE: TokenUsageLike = {
  inputTokens: 1200,
  outputTokens: 80,
  cacheReadTokens: 900,
  cacheWriteTokens: 40,
  totalTokens: 2220,
  reasoningTokens: 12,
};
/** `gen_ai.usage.input_tokens` for USAGE: Cursor's input plus cache read and write. */
export const PROMPT_TOKENS = 2140;

export function events(agentId = AGENT_ID, runId = RUN_ID) {
  const b = base(agentId, runId);
  return {
    request: { type: "request", ...b, request_id: "req-xyz" } as SDKMessageLike,
    running: { type: "status", ...b, status: "RUNNING" } as SDKMessageLike,
    user: { type: "user", ...b, message: { role: "user", content: [{ type: "text", text: `List files ${PROMPT_MARKER}` }] } } as SDKMessageLike,
    system: { type: "system", subtype: "init", ...b, model: MODEL, tools: ["read", "ls", "shell"] } as SDKMessageLike,
    thinkingDelta1: { type: "thinking", ...b, text: "I should list " } as SDKMessageLike,
    thinkingDelta2: { type: "thinking", ...b, text: "files first." } as SDKMessageLike,
    thinkingDone: { type: "thinking", ...b, text: "", thinking_duration_ms: 420 } as SDKMessageLike,
    textDelta1: { type: "assistant", ...b, message: { role: "assistant", content: [{ type: "text", text: "Let me " }] } } as SDKMessageLike,
    textDelta2: { type: "assistant", ...b, message: { role: "assistant", content: [{ type: "text", text: "look." }] } } as SDKMessageLike,
    lsStart: { type: "tool_call", ...b, call_id: "call-ls", name: "ls", status: "running", args: { path: SECRET_ARG } } as SDKMessageLike,
    lsPartial: { type: "tool_call", ...b, call_id: "call-ls", name: "ls", status: "running", args: { path: SECRET_ARG, depth: 1 } } as SDKMessageLike,
    lsDone: {
      type: "tool_call",
      ...b,
      call_id: "call-ls",
      name: "ls",
      status: "completed",
      args: { path: SECRET_ARG, depth: 1 },
      result: { status: "success", value: { files: ["README.md", SECRET_RESULT] } },
      truncated: { args: false, result: false },
    } as SDKMessageLike,
    readStart: { type: "tool_call", ...b, call_id: "call-read", name: "read", status: "running", args: { path: SECRET_ARG } } as SDKMessageLike,
    readError: {
      type: "tool_call",
      ...b,
      call_id: "call-read",
      name: "read",
      status: "error",
      args: { path: SECRET_ARG },
      result: { status: "error", error: `ENOENT ${SECRET_RESULT}` },
    } as SDKMessageLike,
    shellCompletedOnly: {
      type: "tool_call",
      ...b,
      call_id: "call-shell",
      name: "shell",
      status: "completed",
      args: { command: "cat README.md" },
      result: { status: "success", value: { stdout: SECRET_RESULT, exitCode: 0 } },
    } as SDKMessageLike,
    answerDelta: { type: "assistant", ...b, message: { role: "assistant", content: [{ type: "text", text: `README says ${SECRET_RESULT}` }] } } as SDKMessageLike,
    task: { type: "task", ...b, text: "Summarized the folder" } as SDKMessageLike,
    usage: { type: "usage", ...b, usage: USAGE } as SDKMessageLike,
    finished: { type: "status", ...b, status: "FINISHED" } as SDKMessageLike,
    errored: { type: "status", ...b, status: "ERROR", message: "Model provider returned 500" } as SDKMessageLike,
  };
}

/** A full successful run: thinking, text, a tool with a partial update, a failing tool, a completed-only tool, the answer, usage. */
export function fullRun(agentId = AGENT_ID, runId = RUN_ID): Step[] {
  const e = events(agentId, runId);
  return [
    { event: e.request },
    { event: e.running },
    { event: e.user },
    { event: e.system },
    { event: e.thinkingDelta1, delayMs: 2 },
    { event: e.thinkingDelta2, delayMs: 2 },
    { event: e.thinkingDone, delayMs: 2 },
    { event: e.textDelta1, delayMs: 2 },
    { event: e.textDelta2, delayMs: 2 },
    { event: e.lsStart, delayMs: 2 },
    { event: e.lsPartial, delayMs: 2 },
    { event: e.lsDone, delayMs: 3 },
    { event: e.readStart, delayMs: 2 },
    { event: e.readError, delayMs: 2 },
    { event: e.shellCompletedOnly, delayMs: 2 },
    { event: e.answerDelta, delayMs: 2 },
    { event: e.task },
    { event: e.usage },
    { event: e.finished },
    { finish: "finished", result: `README says ${SECRET_RESULT}`, usage: USAGE },
  ];
}

/** A run whose model call fails after one tool started. */
export function failedRun(agentId = AGENT_ID, runId = RUN_ID): Step[] {
  const e = events(agentId, runId);
  return [
    { event: e.running },
    { event: e.lsStart, delayMs: 2 },
    { event: e.errored, delayMs: 2 },
    { finish: "error", error: { message: "Model provider returned 500", code: "provider_error" } },
  ];
}

/** A run that keeps going for a while after the first events (for early-break tests). */
export function slowRun(agentId = AGENT_ID, runId = RUN_ID): Step[] {
  const e = events(agentId, runId);
  return [
    { event: e.running },
    { event: e.lsStart, delayMs: 2 },
    { event: e.textDelta1, delayMs: 30 },
    { event: e.lsDone, delayMs: 2 },
    { event: e.usage, delayMs: 2 },
    { event: e.finished, delayMs: 2 },
    { finish: "finished", result: "done", usage: USAGE },
  ];
}

/** Usage a live local run reported (2026-10-20, @cursor/sdk 1.0.38, composer-2.5): no reasoning tokens. */
export const LIVE_USAGE: TokenUsageLike = {
  inputTokens: 23820,
  outputTokens: 258,
  cacheReadTokens: 19172,
  cacheWriteTokens: 0,
  totalTokens: 43250,
};
export const LIVE_READ_ID = "tool_78fbe67f-22b2-46f8-b1b4-ee84232072e";
export const LIVE_EDIT_ID = "tool_785d444b-6d2c-4c59-ae58-848acbeec51";
export const PLAN_ERROR = `Model unavailable on Plus ${SECRET_RESULT}`;

/**
 * The event sequence a live local run emitted (2026-10-20, @cursor/sdk 1.0.38,
 * composer-2.5): read README.md, write notes.txt, answer. Paths and texts are
 * placeholders. Unlike the documented example above: no `request`, `user`,
 * `system` or `task` events (the request id is only on `RunResult.requestId`),
 * each thinking block ends with an empty-text event carrying
 * `thinking_duration_ms`, a partial `running` update repeats the same args,
 * completed tools carry no `truncated`, and usage has no reasoning tokens.
 */
export function liveRun(agentId = AGENT_ID, runId = RUN_ID): Step[] {
  const b = base(agentId, runId);
  const thinking = (text: string, thinking_duration_ms?: number): Step => ({
    event: { type: "thinking", ...b, text, ...(thinking_duration_ms !== undefined ? { thinking_duration_ms } : {}) } as SDKMessageLike,
    delayMs: 1,
  });
  const tool = (call_id: string, name: string, status: string, result?: unknown): Step => ({
    event: { type: "tool_call", ...b, call_id, name, status, args: { path: SECRET_ARG }, ...(result ? { result } : {}) } as SDKMessageLike,
    delayMs: 2,
  });
  const assistant = (text: string): Step => ({
    event: { type: "assistant", ...b, message: { role: "assistant", content: [{ type: "text", text }] } } as SDKMessageLike,
    delayMs: 1,
  });
  return [
    { event: { type: "status", ...b, status: "RUNNING" } as SDKMessageLike },
    thinking("Reading"),
    thinking(" README.md"),
    thinking("", 62),
    tool(LIVE_READ_ID, "read", "running"),
    tool(LIVE_READ_ID, "read", "completed", { status: "success", value: { content: SECRET_RESULT, totalLines: 3, fileSize: 31 } }),
    thinking("The second line"),
    thinking("", 112),
    tool(LIVE_EDIT_ID, "edit", "running"),
    tool(LIVE_EDIT_ID, "edit", "running"),
    tool(LIVE_EDIT_ID, "edit", "completed", { status: "success", value: { linesAdded: 1, linesRemoved: 0, diffString: `+${SECRET_RESULT}` } }),
    thinking("Created notes.txt"),
    thinking("", 235),
    assistant("Created notes.txt "),
    assistant(`with ${SECRET_RESULT}`),
    { event: { type: "usage", ...b, usage: LIVE_USAGE } as SDKMessageLike },
    { event: { type: "status", ...b, status: "FINISHED" } as SDKMessageLike },
    { finish: "finished", result: `Created notes.txt with ${SECRET_RESULT}`, usage: LIVE_USAGE },
  ];
}

/**
 * Parallel tool calls from a live run (2026-10-20): `shell` starts, `read`
 * starts and completes, then `shell` completes. A shell command that exits
 * non-zero is still a `completed` call with `status: "success"`.
 */
export function liveParallelToolsRun(agentId = AGENT_ID, runId = RUN_ID): Step[] {
  const b = base(agentId, runId);
  const tool = (call_id: string, name: string, status: string, args: unknown, result?: unknown, delayMs = 2): Step => ({
    event: { type: "tool_call", ...b, call_id, name, status, args, ...(result ? { result } : {}) } as SDKMessageLike,
    delayMs,
  });
  return [
    { event: { type: "status", ...b, status: "RUNNING" } as SDKMessageLike },
    tool("tool_shell", "shell", "running", { command: `ls -la ${SECRET_ARG}`, timeout: 30000 }),
    tool("tool_read", "read", "running", { path: SECRET_ARG }),
    tool("tool_read", "read", "completed", { path: SECRET_ARG }, { status: "success", value: { content: SECRET_RESULT } }),
    tool("tool_shell", "shell", "completed", { command: `ls -la ${SECRET_ARG}`, timeout: 30000 }, { status: "success", value: { exitCode: 1, stdout: "", stderr: SECRET_RESULT } }, 15),
    { event: { type: "status", ...b, status: "FINISHED" } as SDKMessageLike },
    { finish: "finished", result: "done", usage: LIVE_USAGE },
  ];
}

/**
 * A live run with a model setting the plan does not include (2026-10-20,
 * composer-2.5 without `fast: false` on Plus): the run starts, then fails
 * with a status message and a `RunResult.error` that has no code.
 */
export function liveModelUnavailableRun(agentId = AGENT_ID, runId = RUN_ID): Step[] {
  const b = base(agentId, runId);
  return [
    { event: { type: "status", ...b, status: "RUNNING" } as SDKMessageLike },
    { event: { type: "status", ...b, status: "ERROR", message: PLAN_ERROR } as SDKMessageLike, delayMs: 2 },
    { finish: "error", error: { message: PLAN_ERROR } },
  ];
}
