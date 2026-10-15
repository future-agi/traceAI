/**
 * Event sequences in the order `@earendil-works/pi-coding-agent` 1.2.0
 * emits them from `AgentSession.subscribe()`.
 *
 * `happyPath` mirrors a recorded real run (gpt-6-luna through an
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
    provider: "cliproxy",
    model: "gpt-6-luna",
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
    ...toolTurn(T0 + 3_000, "call_read_9", "read", { path: ARG_SECRET }, TOOL_ERROR_SECRET, usage(900, 30), true),
    ...finalTurn(T0 + 5_000, OUTPUT_SECRET, usage(950, 20)),
    { type: "agent_end", messages: [], willRetry: false },
    { type: "auto_retry_end", success: true, attempt: 1 },
    { type: "agent_settled", aborted: false },
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

/** Minimal stand-in for `AgentSession`: `sessionId`, `subscribe()`, `dispose()`. */
export class FakeSession implements PiSessionLike {
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
