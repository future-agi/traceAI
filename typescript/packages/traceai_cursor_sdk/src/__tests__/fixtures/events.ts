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

export const USAGE: TokenUsageLike = {
  inputTokens: 1200,
  outputTokens: 80,
  cacheReadTokens: 900,
  cacheWriteTokens: 40,
  totalTokens: 1280,
  reasoningTokens: 12,
};

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
