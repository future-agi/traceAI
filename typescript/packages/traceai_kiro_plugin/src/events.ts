import { redact, type ResolvedConfig } from "@traceai/agent-core";

export const EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"] as const;
export type EventName = typeof EVENTS[number];
export interface KiroEvent {
  event: EventName;
  sessionId: string;
  time: number;
  input?: string;
  output?: string;
  tool?: string;
  failed?: boolean;
}
export const record = (v: unknown): Record<string, unknown> =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {};

/** Content is filtered BEFORE persistence, not only at OTLP export. Never export cwd or arbitrary fields. */
export function text(value: unknown, config: ResolvedConfig, output = false, error = false): string | undefined {
  if (!config.captureContent || (output ? config.hideOutputs : config.hideInputs) || (error && config.hideErrorDetails)) return;
  if (value === undefined) return;
  let result = typeof value === "string" ? value : JSON.stringify(value);
  for (const secret of [config.credentials?.apiKey, config.credentials?.secretKey])
    if (secret) result = result.split(secret).join("[REDACTED]");
  result = redact(result).slice(0, 20000);
  // Bound serialized UTF-8 as well as characters (emoji/control characters otherwise overflow state).
  if (Buffer.byteLength(JSON.stringify(result)) > 20000) {
    let low = 0, high = result.length;
    while (low < high) {
      const mid = Math.ceil((low + high) / 2);
      if (Buffer.byteLength(JSON.stringify(result.slice(0, mid))) <= 20000) low = mid;
      else high = mid - 1;
    }
    result = result.slice(0, low).replace(/[\uD800-\uDBFF]$/, "");
  }
  return result;
}
/** V3 PascalCase only. Legacy aliases are deliberately not registered as a second profile. */
export function parseEvent(trigger: string, payload: unknown, config: ResolvedConfig): KiroEvent | undefined {
  const p = record(payload);
  if (!EVENTS.includes(trigger as EventName) || (p.hook_event_name !== undefined && p.hook_event_name !== trigger)) return;
  if (typeof p.session_id !== "string" || !p.session_id || p.session_id.length > 256) return;
  const event: KiroEvent = { event: trigger as EventName, sessionId: p.session_id, time: Date.now() };
  if (!config.enabled) return event;
  if (trigger === "UserPromptSubmit") event.input = text(p.prompt, config);
  if (trigger === "Stop") event.output = text(p.assistant_response, config, true);
  if (trigger === "PostToolUse") {
    event.tool = typeof p.tool_name === "string" && /^[A-Za-z0-9_.:/-]{1,128}$/.test(p.tool_name) && !p.tool_name.includes("://") ? p.tool_name : "tool";
    event.failed = record(p.tool_response).success === false;
    event.input = text(p.tool_input, config);
    event.output = text(p.tool_response, config, true, event.failed);
  }
  // No documented call id: PreToolUse cannot safely pair parallel tools or provide durations.
  // No model requests, usage, cost, subagent ancestry or transcript paths are inferred.
  return event;
}
