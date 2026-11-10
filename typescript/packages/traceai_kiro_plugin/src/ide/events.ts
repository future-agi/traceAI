import { redact, type ResolvedConfig } from "@traceai/agent-core";

export const IDE_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "PostFileCreate", "PostFileSave", "PostFileDelete", "PreTaskExec", "PostTaskExec", "PreCompact", "PostCompact"] as const;
export type IdeEventName = typeof IDE_EVENTS[number];
export interface IdeEvent {
  event: IdeEventName;
  session: string;
  time: number;
  input?: string;
  output?: string;
  tool?: string;
  failed?: boolean;
}
const record = (v: unknown): Record<string, unknown> =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {};
const identity = (v: unknown): string | undefined =>
  typeof v === "string" && v.length > 0 && v.length <= 256 && !/[\x00-\x1f]/.test(v) ? v : undefined;
function text(v: unknown, config: ResolvedConfig, output = false, error = false): string | undefined {
  if (v === undefined || !config.captureContent || (output ? config.hideOutputs : config.hideInputs) || (error && config.hideErrorDetails)) return;
  let value = typeof v === "string" ? v : JSON.stringify(v);
  for (const secret of [config.credentials?.apiKey, config.credentials?.secretKey])
    if (secret) value = value.split(secret).join("[REDACTED]");
  return redact(value).slice(0, 20000);
}
/** Serializer contract from official IDE 1.4.121, extension.js v8t, NOT a live hook capture.
 * Missing session identity drops the event. This version has no turn_id, tool_use_id,
 * model usage or Stop assistant_response; never ingest lookalike fields from CLI fixtures.
 * USER_PROMPT is the documented legacy IDE fallback. No transcript paths are read.
 */
export function parseIdeEvent(trigger: string, raw: unknown, config: ResolvedConfig, userPrompt?: string): IdeEvent | undefined {
  const p = record(raw);
  if (!IDE_EVENTS.includes(trigger as IdeEventName) || (p.hook_event_name !== undefined && p.hook_event_name !== trigger)) return;
  const session = identity(p.session_id);
  if (!session) return;
  const e: IdeEvent = { event: trigger as IdeEventName, session, time: Date.now() };
  if (trigger === "UserPromptSubmit") e.input = text(p.prompt ?? userPrompt, config);
  if (trigger === "PostToolUse") {
    const tool = identity(p.tool_name);
    e.tool = tool && /^[A-Za-z0-9_.:/-]{1,128}$/.test(tool) && !tool.includes("://") && ![config.credentials?.apiKey, config.credentials?.secretKey].some(k => k && tool.includes(k)) ? tool : "tool";
    const result = record(p.tool_response);
    e.failed = result.success === false || result.is_error === true ? true
      : result.success === true || result.is_error === false ? false : undefined;
    e.input = text(p.tool_input, config);
    e.output = text(p.tool_response, config, true, e.failed);
  }
  if (["PostFileCreate", "PostFileSave", "PostFileDelete"].includes(trigger))
    e.input = text({ file_path: p.file_path }, config);
  if (["PreTaskExec", "PostTaskExec"].includes(trigger)) {
    e.input = text({ spec_name: p.spec_name, task_name: p.task_name }, config);
    if (typeof p.task_success === "boolean") e.failed = !p.task_success;
  }
  if (["PreCompact", "PostCompact"].includes(trigger))
    e.input = text({ reason: p.reason, message_count: p.message_count }, config);
  // Compaction summary/transcript paths are deliberately not read or treated as LLM requests.
  return e;
}
