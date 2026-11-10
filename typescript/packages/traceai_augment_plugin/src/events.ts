import { redact, type ResolvedConfig, type TurnOutcome } from "@traceai/agent-core";

export interface ToolRecord { name: string; input?: string; output?: string; outcome: TurnOutcome; changes: { edit: number; create: number; delete: number; other: number } }
export interface EventRecord {
  event: "SessionStart" | "PostToolUse" | "Stop" | "SessionEnd";
  session: string;
  tool?: ToolRecord;
  input?: string;
  output?: string;
  outcome?: TurnOutcome;
}
const object = (x: unknown): Record<string, unknown> | undefined => x !== null && typeof x === "object" && !Array.isArray(x) ? x as Record<string, unknown> : undefined;
export function content(value: unknown, config: ResolvedConfig): string | undefined {
  if (value === undefined) return undefined;
  let text = typeof value === "string" ? value : JSON.stringify(value);
  for (const key of [config.credentials?.apiKey, config.credentials?.secretKey])
    if (key) text = text.split(key).join("[REDACTED]");
  return redact(text).slice(0, 20000);
}
function toolName(value: unknown, config: ResolvedConfig): string {
  return typeof value === "string" && /^[a-zA-Z0-9_.:-]{1,128}$/.test(value) && ![config.credentials?.apiKey, config.credentials?.secretKey].some((key) => key && value.includes(key)) ? value : "tool";
}
/** Allowlist only the documented 0.x surface; never persist raw payloads or undocumented _exchange. */
export function normalize(payload: unknown, config: ResolvedConfig): EventRecord | undefined {
  const p = object(payload);
  if (!p || typeof p.conversation_id !== "string" || !p.conversation_id || p.conversation_id.length > 512) return undefined;
  if (!["SessionStart", "PostToolUse", "Stop", "SessionEnd"].includes(String(p.hook_event_name))) return undefined;
  const result: EventRecord = { event: p.hook_event_name as EventRecord["event"], session: p.conversation_id };
  if (result.event === "PostToolUse") {
    const failed = p.tool_error !== undefined && p.tool_error !== null && p.tool_error !== "";
    const changes = { edit: 0, create: 0, delete: 0, other: 0 };
    const files: Record<string, unknown>[] = [];
    if (Array.isArray(p.file_changes)) for (const raw of p.file_changes.slice(0, 128)) {
      const item = object(raw);
      if (!item) continue;
      const kind = item.changeType === "edit" || item.changeType === "create" || item.changeType === "delete" ? item.changeType : "other";
      changes[kind]++;
      files.push({ changeType: kind, path: item.path, content: item.content, oldContent: item.oldContent });
    }
    const output = failed ? p.tool_error : files.length ? { result: p.tool_output, fileChanges: files } : p.tool_output;
    result.tool = { name: toolName(p.tool_name, config), outcome: failed ? "error" : "success", changes,
      input: config.hideInputs ? undefined : content(p.tool_input, config),
      output: config.hideOutputs || (failed && config.hideErrorDetails) ? undefined : content(output, config),
    };
  }
  if (result.event === "Stop") {
    const conversation = object(p.conversation);
    result.input = config.hideInputs ? undefined : content(conversation?.userPrompt, config);
    result.outcome = p.agent_stop_cause === "end_turn" ? "success" : p.agent_stop_cause === "error" ? "error" : p.agent_stop_cause === "interrupted" ? "interrupted" : "incomplete";
    result.output = config.hideOutputs || (result.outcome === "error" && config.hideErrorDetails) ? undefined : content(conversation?.agentTextResponse, config);
  }
  return result;
}
