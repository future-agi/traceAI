/** Convert the SDK's callback/conversation shapes to the same events as stream(). */
import { SDKMessageLike, TokenUsageLike } from "./types";

export function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined;
}

export function isTask(name: unknown): boolean {
  return typeof name === "string" && /^(task|agent)$/i.test(name);
}

function toolEvent(value: unknown, callId?: unknown, status?: string): SDKMessageLike | undefined {
  const tool = record(value);
  if (!tool || typeof tool.type !== "string") return undefined;
  const result = record(tool.result);
  return {
    type: "tool_call", name: tool.type,
    call_id: typeof callId === "string" ? callId : typeof tool.callId === "string" ? tool.callId : undefined,
    status: status === "running" ? status : result?.status === "error" ? "error" : "completed",
    args: tool.args, result: tool.result,
  };
}

export function stepEvent(value: unknown): SDKMessageLike | undefined {
  const step = record(value);
  const message = record(step?.message);
  if (!step || !message) return undefined;
  switch (step.type) {
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
