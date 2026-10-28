/**
 * Helpers that turn Pi message content into span text. Only called when the
 * content policy allows the value to be exported.
 */
import { PiContentPart, PiMessage } from "./types";

export interface ContentPolicy {
  /** Drop prompts and tool arguments. */
  hideInputs: boolean;
  /** Drop assistant text and tool results. Tool-call arguments follow `hideInputs`. */
  hideOutputs: boolean;
  /** Drop error text independently of ordinary output capture. */
  hideErrorDetails: boolean;
  /** Longest exported text value, in characters. Longer values are cut and marked. */
  maxContentLength: number;
}

export const DEFAULT_MAX_CONTENT_LENGTH = 16_384;

export function truncate(value: string, maxLength: number): string {
  if (maxLength <= 0 || value.length <= maxLength) {
    return value;
  }
  return `${value.slice(0, maxLength)}... [truncated ${value.length - maxLength} chars]`;
}

/** Text of a message: string content, or its text parts joined. Images become `[image]`. */
export function messageText(content: PiMessage["content"] | unknown): string {
  if (typeof content === "string") {
    return content;
  }
  if (!Array.isArray(content)) {
    return "";
  }
  const parts: string[] = [];
  for (const part of content as PiContentPart[]) {
    if (!part || typeof part !== "object") continue;
    if (part.type === "text" && typeof part.text === "string") {
      parts.push(part.text);
    } else if (part.type === "image") {
      parts.push("[image]");
    }
  }
  return parts.join("\n");
}

/** Tool calls in an assistant message (`ToolCall` parts). */
export function toolCalls(message: PiMessage): PiContentPart[] {
  if (!Array.isArray(message.content)) {
    return [];
  }
  return message.content.filter((part) => part && typeof part === "object" && part.type === "toolCall");
}

/** JSON for span attributes; never throws. */
export function safeJson(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  try {
    return JSON.stringify(value) ?? "";
  } catch {
    return String(value);
  }
}

/** Text of a tool result (`AgentToolResult` has `content` parts, like a message). */
export function toolResultText(result: unknown): string {
  if (result && typeof result === "object" && "content" in result) {
    const text = messageText((result as { content?: unknown }).content);
    if (text) return text;
  }
  return typeof result === "string" ? result : safeJson(result);
}
