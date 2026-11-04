/** Convert the SDK's callback/conversation shapes to the same events as stream(). */
import { SDKMessageLike, TokenUsageLike } from "./types";

export function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined;
}

export function isTask(name: unknown): boolean {
  return typeof name === "string" && /^(task|agent)$/i.test(name);
}

// Known protobuf JSON oneofs corresponding to the SDK's ToolCall union. Do
// not infer arbitrary *ToolCall fields or walk unknown objects as transcripts.
const PROTO_TOOL_TYPES = {
  shellToolCall: "shell", writeToolCall: "write", deleteToolCall: "delete",
  globToolCall: "glob", grepToolCall: "grep", readToolCall: "read",
  editToolCall: "edit", lsToolCall: "ls", readLintsToolCall: "readLints",
  mcpToolCall: "mcp", generateImageToolCall: "generateImage",
  recordScreenToolCall: "recordScreen", computerUseToolCall: "computerUse",
  semSearchToolCall: "semSearch", createPlanToolCall: "createPlan",
  updateTodosToolCall: "updateTodos", taskToolCall: "task",
} as const;

function normalizeResult(value: unknown): unknown {
  const result = record(value);
  if (!result || result.status === "success" || result.status === "error") return value;
  // Prefer an error if a malformed oneof supplies both variants. Never mutate
  // the result object returned to the caller by the SDK.
  if (Object.prototype.hasOwnProperty.call(result, "error")) return { status: "error", error: result.error };
  if (Object.prototype.hasOwnProperty.call(result, "success")) return { status: "success", value: result.success };
  return value;
}

function normalizeTool(value: unknown): Record<string, unknown> | undefined {
  const tool = record(value);
  if (!tool || typeof tool.type === "string") return tool;
  for (const [field, type] of Object.entries(PROTO_TOOL_TYPES)) {
    const body = record(tool[field]);
    if (body) return { type, callId: tool.callId ?? body.callId, args: body.args, result: body.result };
  }
  return undefined;
}

function toolEvent(value: unknown, callId?: unknown, status?: string): SDKMessageLike | undefined {
  const tool = normalizeTool(value);
  if (!tool || typeof tool.type !== "string") return undefined;
  const result = normalizeResult(tool.result);
  return {
    type: "tool_call", name: tool.type,
    call_id: typeof callId === "string" ? callId : typeof tool.callId === "string" ? tool.callId : undefined,
    status: status === "running" ? status : record(result)?.status === "error" ? "error" : "completed",
    args: tool.args, result,
  };
}

export function stepEvent(value: unknown): SDKMessageLike | undefined {
  const step = record(value);
  if (!step) return undefined;
  // TaskSuccess.conversationSteps is unknown[]: unlike onStep callbacks it
  // can retain protobuf JSON oneofs instead of { type, message } envelopes.
  const type = typeof step.type === "string" ? step.type
    : ["toolCall", "assistantMessage", "thinkingMessage"].find(key => record(step[key]));
  const message = record(typeof step.type === "string" ? step.message : type ? step[type] : undefined);
  if (!message) return undefined;
  switch (type) {
    case "toolCall": return toolEvent(message);
    case "assistantMessage":
      return typeof message.text === "string"
        ? { type: "assistant", message: { content: [{ type: "text", text: message.text }] } } : undefined;
    case "thinkingMessage":
      return { type: "thinking", text: typeof message.text === "string" ? message.text : undefined,
        thinking_duration_ms: typeof message.thinkingDurationMs === "number" ? message.thinkingDurationMs : undefined };
    default: return undefined;
  }
}

export function deltaEvent(value: unknown): SDKMessageLike | undefined {
  const update = record(value);
  if (!update) return undefined;
  switch (update.type) {
    case "tool-call-started":
    case "partial-tool-call": return toolEvent(update.toolCall, update.callId, "running");
    case "tool-call-completed": return toolEvent(update.toolCall, update.callId);
    case "text-delta":
      return typeof update.text === "string"
        ? { type: "assistant", message: { content: [{ type: "text", text: update.text }] } } : undefined;
    case "thinking-delta": return { type: "thinking", text: typeof update.text === "string" ? update.text : undefined };
    case "thinking-completed": return { type: "thinking", thinking_duration_ms: typeof update.thinkingDurationMs === "number" ? update.thinkingDurationMs : undefined };
    case "turn-ended": return { type: "usage", usage: record(update.usage) as TokenUsageLike | undefined };
    default: return undefined;
  }
}
