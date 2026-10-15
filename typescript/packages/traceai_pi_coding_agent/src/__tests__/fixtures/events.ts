/**
 * Event sequences in the order `@earendil-works/pi-coding-agent` 1.2.0
 * emits them from `AgentSession.subscribe()`.
 *
 * `happyPath` mirrors a recorded real run (a model behind an
 * OpenAI-compatible endpoint, prompt "list the files, read README.md"): same
 * event types, order, roles, message keys, stop reasons and usage shape. Text,
 * ids and token numbers are fixtures; the *_SECRET strings let tests prove
 * which content leaves the process.
 */
import { PiSessionEvent, PiSessionEventListener, PiSessionLike } from "../../types";

export const SESSION_ID = "0199f1d2-7c1e-7000-8000-00000000c0de";
export const PROMPT_SECRET = "PROMPT_SECRET_list_files_please";
export const SYSTEM_SECRET = "SYSTEM_SECRET_you_are_pi";
export const ARG_SECRET = "ARG_SECRET_/tmp/project";
export const RESULT_SECRET = "RESULT_SECRET_README.md_hello.js";
export const OUTPUT_SECRET = "OUTPUT_SECRET_the_title_is_Throwaway";
export const TOOL_ERROR_SECRET = "TOOL_ERROR_SECRET_ENOENT_missing.txt";
/** A local path with a username, as provider and tool errors carry them. */
export const LOCAL_USER = "jdoe-secret-user";
export const LOCAL_PATH = `/Users/${LOCAL_USER}/work/project/PROVIDER_ERROR_SECRET.md`;
export const PROVIDER_ERROR_TEXT = `400 {"type":"error","error":{"type":"invalid_request_error","message":"cannot read ${LOCAL_PATH} for ${LOCAL_USER}"}}`;
export const TOOL_PATH_ERROR = `ENOENT: no such file or directory, access '/Users/${LOCAL_USER}/work/project/TOOL_PATH_SECRET.txt'`;
export const SESSION_NAME_SECRET = "SESSION_NAME_SECRET_fix-billing-bug";
export const SUMMARY_SECRET = "SUMMARY_SECRET_the_user_asked_about_billing";

const T0 = 1_760_000_000_000;

function usage(input: number, output: number, cacheRead = 0, cacheWrite = 0, cost = 0) {
  return {
    input,
    output,
    cacheRead,
    cacheWrite,
    totalTokens: input + output + cacheRead + cacheWrite,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: cost },
  };
}

function assistant(
  timestamp: number,
  content: unknown[],
  stopReason: string,
  extra: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    role: "assistant",
    content,
    api: "openai-completions",
    provider: "my-gateway",
    model: "example-model",
    responseId: `chatcmpl-${timestamp}`,
    timestamp,
    usage: usage(0, 0),
    stopReason: "pending",
    ...extra,
    ...(stopReason === "pending" ? {} : { stopReason }),
  };
}

function toolTurn(
  start: number,
  callId: string,
  toolName: string,
  args: Record<string, unknown>,
  resultText: string,
  tokens: ReturnType<typeof usage>,
  isError = false,
): PiSessionEvent[] {
  const call = [{ type: "toolCall", id: callId, name: toolName, arguments: args }];
  const streaming = assistant(start, call, "pending");
  const done = assistant(start, call, "toolUse", { usage: tokens, durationMs: 900, thinkingLevel: "off", rawStopReason: "tool_calls" });
  const toolResult = {
    role: "toolResult",
    toolCallId: callId,
    toolName,
    content: [{ type: "text", text: resultText }],
    isError,
    durationMs: 20,
    timestamp: start + 1_000,
  };
  return [
    { type: "turn_start" },
    { type: "message_start", message: streaming },
    { type: "message_update", message: streaming, assistantMessageEvent: { type: "toolcall_delta", delta: "{" } },
    { type: "message_end", message: done },
    { type: "tool_execution_start", toolCallId: callId, toolName, args },
    { type: "tool_execution_update", toolCallId: callId, toolName, args, partialResult: { content: [] } },
    { type: "tool_execution_end", toolCallId: callId, toolName, result: { content: [{ type: "text", text: resultText }] }, isError, durationMs: 20 },
    { type: "message_start", message: toolResult },
    { type: "message_end", message: toolResult },
    { type: "turn_end", message: done, toolResults: [toolResult] },
  ];
}

function finalTurn(start: number, text: string, tokens: ReturnType<typeof usage>): PiSessionEvent[] {
  const streaming = assistant(start, [{ type: "text", text: "" }], "pending");
  const done = assistant(start, [{ type: "text", text }], "stop", { usage: tokens, durationMs: 800, thinkingLevel: "off", rawStopReason: "stop" });
  return [
    { type: "turn_start" },
    { type: "message_start", message: streaming },
    { type: "message_update", message: streaming, assistantMessageEvent: { type: "text_delta", delta: text } },
    { type: "message_end", message: done },
    { type: "turn_end", message: done, toolResults: [] },
  ];
}

function promptMessages(): PiSessionEvent[] {
  const system = { role: "system", content: SYSTEM_SECRET, sections: {}, toolsAdded: [], timestamp: T0 - 3 };
  const user = { role: "user", content: [{ type: "text", text: PROMPT_SECRET }], timestamp: T0 - 3 };
  return [
    { type: "message_start", message: system },
    { type: "message_end", message: system },
    { type: "message_start", message: user },
    { type: "message_end", message: user },
  ];
}

/** agent_start → 2 tool turns (ls, read) → final text turn → agent_end → agent_settled. */
export function happyPath(): PiSessionEvent[] {
  const [turnStart, ...rest] = toolTurn(T0 + 258, "call_ls_1", "ls", { path: ARG_SECRET }, RESULT_SECRET, usage(1182, 75));
  return [
    { type: "agent_start" },
    turnStart,
    ...promptMessages(),
    ...rest,
    ...toolTurn(T0 + 6_490, "call_read_2", "read", { path: "README.md" }, RESULT_SECRET, usage(1000, 48, 244, 0, 0.0125)),
    ...finalTurn(T0 + 11_048, OUTPUT_SECRET, usage(1325, 35)),
    { type: "agent_end", messages: [], willRetry: false },
    { type: "agent_settled", aborted: false },
  ];
}

/**
 * A provider error, Pi's automatic retry, then a turn whose tool fails, then
 * a final answer.
 */
export function retryAndToolError(): PiSessionEvent[] {
  const failedStreaming = assistant(T0 + 100, [], "pending");
  const failed = assistant(T0 + 100, [], "error", {
    usage: usage(0, 0),
    durationMs: 300,
    errorMessage: "429 Too Many Requests: rate limited",
  });
  // Pi 1.2.0 emits auto_retry_end right after the first successful message_end.
  const retried = toolTurn(T0 + 3_000, "call_read_9", "read", { path: ARG_SECRET }, TOOL_ERROR_SECRET, usage(900, 30), true);
  const firstEnd = retried.findIndex((event) => event.type === "message_end");
  retried.splice(firstEnd + 1, 0, { type: "auto_retry_end", success: true, attempt: 1 });
  return [
    { type: "agent_start" },
    { type: "turn_start" },
    ...promptMessages(),
    { type: "message_start", message: failedStreaming },
    { type: "message_end", message: failed },
    { type: "turn_end", message: failed, toolResults: [] },
    { type: "agent_end", messages: [], willRetry: true },
    { type: "auto_retry_start", attempt: 1, maxAttempts: 3, delayMs: 2000, errorMessage: "429 Too Many Requests: rate limited" },
    { type: "agent_start" },
    ...retried,
    ...finalTurn(T0 + 5_000, OUTPUT_SECRET, usage(950, 20)),
    { type: "agent_end", messages: [], willRetry: false },
    { type: "agent_settled", aborted: false },
  ];
}

/**
 * A provider error whose text carries a local path and username, and a tool
 * error that does too. Pi gives up (no retry).
 */
export function providerAndToolPathErrors(): PiSessionEvent[] {
  const toolEvents = toolTurn(T0 + 100, "call_read_p", "read", { path: "missing.txt" }, TOOL_PATH_ERROR, usage(400, 10), true);
  const failedStreaming = assistant(T0 + 2_000, [], "pending");
  const failed = assistant(T0 + 2_000, [], "error", { usage: usage(0, 0), durationMs: 120, errorMessage: PROVIDER_ERROR_TEXT });
  return [
    { type: "agent_start" },
    ...toolEvents,
    { type: "turn_start" },
    { type: "message_start", message: failedStreaming },
    { type: "message_end", message: failed },
    { type: "turn_end", message: failed, toolResults: [] },
    { type: "agent_end", messages: [], willRetry: false },
    { type: "agent_settled", aborted: false },
  ];
}

/**
 * One prompt as Pi before 0.80.5 emits it: the same events as `happyPath`
 * but no `agent_settled` (it did not exist yet).
 */
export function legacyPrompt(): PiSessionEvent[] {
  return happyPath().filter((event) => event.type !== "agent_settled");
}

/** A legacy run that Pi retries: agent_end announces the retry with willRetry. */
export function legacyRetry(): PiSessionEvent[] {
  return retryAndToolError().filter((event) => event.type !== "agent_settled");
}

/**
 * A provider error whose automatic retry is cancelled during the backoff
 * (`session.abort()` or `session.abortRetry()`), in the order Pi 0.75.4 to
 * 0.80.3 emit it: agent_end announces the retry, then `auto_retry_end` with
 * `success: false` and no further `agent_start`. Pi 0.80.5+ then also emits
 * `agent_settled`.
 */
export function cancelledRetry(): PiSessionEvent[] {
  const failedStreaming = assistant(T0 + 100, [], "pending");
  const failed = assistant(T0 + 100, [], "error", {
    usage: usage(0, 0),
    durationMs: 300,
    errorMessage: '503 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
  });
  return [
    { type: "agent_start" },
    { type: "turn_start" },
    ...promptMessages(),
    { type: "message_start", message: failedStreaming },
    { type: "message_end", message: failed },
    { type: "turn_end", message: failed, toolResults: [] },
    { type: "agent_end", messages: [], willRetry: true },
    { type: "auto_retry_start", attempt: 1, maxAttempts: 3, delayMs: 3000, errorMessage: "503 overloaded" },
    { type: "auto_retry_end", success: false, attempt: 1, finalError: "Retry cancelled" },
  ];
}

/**
 * Legacy Pi when every retry fails: the last agent_end has `willRetry: false`,
 * then `auto_retry_end` with `success: false` reports the final failure.
 */
export function legacyRetriesExhausted(): PiSessionEvent[] {
  const events = cancelledRetry();
  const finalEnd = events.pop()!;
  const failed = events.find(
    (event) => event.type === "message_end" && (event.message as { role?: string }).role === "assistant",
  )!.message;
  return [
    ...events,
    { type: "agent_start" },
    { type: "turn_start" },
    { type: "message_start", message: failed },
    { type: "message_end", message: failed },
    { type: "turn_end", message: failed, toolResults: [] },
    { type: "agent_end", messages: [], willRetry: false },
    { ...finalEnd, finalError: "503 overloaded" },
  ];
}

/**
 * Pi 0.80.5+: after agent_end, a message queued by an agent_end handler makes
 * Pi continue the same run (a second agent_start) before agent_settled.
 */
export function continuedRun(): PiSessionEvent[] {
  const events = happyPath();
  const settled = events.pop()!;
  return [
    ...events,
    { type: "agent_start" },
    ...finalTurn(T0 + 20_000, OUTPUT_SECRET, usage(1400, 12)),
    { type: "agent_end", messages: [], willRetry: false },
    settled,
  ];
}

/** Threshold compaction after the run's last model call, inside the run (Pi 1.2.0 order). */
export function compactionInRun(): PiSessionEvent[] {
  const events = happyPath();
  const settled = events.pop()!;
  return [
    ...events,
    { type: "compaction_start", reason: "threshold" },
    {
      type: "compaction_end",
      reason: "threshold",
      result: {
        summary: SUMMARY_SECRET,
        firstKeptEntryId: "e1",
        tokensBefore: 180_000,
        estimatedTokensAfter: 12_000,
        usage: usage(5000, 700, 1000, 0, 0.02),
      },
      aborted: false,
      willRetry: false,
    },
    settled,
  ];
}

/** `session.compact()` between prompts: no run is open. */
export function manualCompaction(errorMessage?: string): PiSessionEvent[] {
  return [
    { type: "compaction_start", reason: "manual" },
    {
      type: "compaction_end",
      reason: "manual",
      result: errorMessage ? undefined : { summary: SUMMARY_SECRET, tokensBefore: 9_000, usage: usage(800, 90) },
      aborted: false,
      willRetry: false,
      ...(errorMessage ? { errorMessage } : {}),
    },
  ];
}

/** The user aborts while the model is streaming. */
export function aborted(): PiSessionEvent[] {
  const streaming = assistant(T0 + 100, [{ type: "text", text: "partial" }], "pending");
  const stopped = assistant(T0 + 100, [{ type: "text", text: "partial" }], "aborted", {
    usage: usage(500, 3),
    durationMs: 150,
    errorMessage: "Request was aborted",
  });
  return [
    { type: "agent_start" },
    { type: "turn_start" },
    ...promptMessages(),
    { type: "message_start", message: streaming },
    { type: "message_end", message: stopped },
    { type: "turn_end", message: stopped, toolResults: [] },
    { type: "agent_end", messages: [], willRetry: false },
    { type: "agent_settled", aborted: true },
  ];
}

/** A tool (e.g. codemode) that calls another tool through `ctx.executeTool()`. */
export function nestedTool(): PiSessionEvent[] {
  return [
    { type: "agent_start" },
    { type: "turn_start" },
    { type: "tool_execution_start", toolCallId: "outer", toolName: "codemode", args: { code: ARG_SECRET } },
    { type: "tool_execution_start", toolCallId: "inner", toolName: "read", args: { path: ARG_SECRET }, parentToolCallId: "outer" },
    { type: "tool_execution_end", toolCallId: "inner", toolName: "read", result: { content: [{ type: "text", text: RESULT_SECRET }] }, isError: false, durationMs: 5, parentToolCallId: "outer" },
    { type: "tool_execution_end", toolCallId: "outer", toolName: "codemode", result: { content: [{ type: "text", text: RESULT_SECRET }] }, isError: false, durationMs: 9 },
    { type: "turn_end", message: assistant(T0, [], "stop", { usage: usage(1, 1) }), toolResults: [] },
    { type: "agent_end", messages: [], willRetry: false },
    { type: "agent_settled", aborted: false },
  ];
}

/** Stops mid-tool: the session is disposed before Pi finishes. */
export function interrupted(): PiSessionEvent[] {
  return happyPath().slice(0, 10);
}

/**
 * Pi < 0.80.5 stand-in: `sessionId`, `subscribe()`, `dispose()`, no
 * `waitForIdle()` and no `agent_settled`.
 */
export class LegacySession implements PiSessionLike {
  readonly sessionId: string;
  sessionName?: string;
  private listeners: PiSessionEventListener[] = [];
  disposed = false;

  constructor(sessionId: string = SESSION_ID) {
    this.sessionId = sessionId;
  }

  subscribe(listener: PiSessionEventListener): () => void {
    this.listeners.push(listener);
    return () => {
      this.listeners = this.listeners.filter((l) => l !== listener);
    };
  }

  emit(events: PiSessionEvent[]): void {
    for (const event of events) {
      for (const listener of [...this.listeners]) listener(event);
    }
  }

  get listenerCount(): number {
    return this.listeners.length;
  }

  dispose(): void {
    this.disposed = true;
    this.listeners = [];
  }
}

/** Pi 1.2.0 stand-in: adds `waitForIdle()`, which came with `agent_settled` in 0.80.5. */
export class FakeSession extends LegacySession {
  async waitForIdle(): Promise<void> {}
}
