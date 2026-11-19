import { redactStructured } from "@traceai/agent-core/privacy";
import type {
  ResolvedConfig,
  TurnOutcome,
  UsageInput,
} from "@traceai/agent-core";

export const HOOKS = [
  "SessionStart",
  "UserPromptSubmit",
  "PreToolUse",
  "PostToolUse",
  "PostToolUseFailure",
  "PermissionDenied",
  "Stop",
  "StopFailure",
  "StopCancelled",
  "SubagentStart",
  "SubagentStop",
  "PostCompact",
  "SessionEnd",
  "Notification",
] as const;
export type HookName = (typeof HOOKS)[number];
const aliases = Object.fromEntries(
  HOOKS.map((name) => [
    name.replace(/([a-z])([A-Z])/g, "$1_$2").toLowerCase(),
    name,
  ]),
);
export const record = (v: unknown): Record<string, any> =>
  v && typeof v === "object" && !Array.isArray(v)
    ? (v as Record<string, any>)
    : {};
export const id = (v: unknown): string | undefined =>
  typeof v === "string" && /^[a-zA-Z0-9_.:-]{1,256}$/.test(v) ? v : undefined;
export const count = (v: unknown): number | undefined =>
  Number.isSafeInteger(v) && (v as number) >= 0 ? (v as number) : undefined;
export function text(
  v: unknown,
  config: ResolvedConfig,
  output = false,
  error = false,
): string | undefined {
  if (
    (output ? config.hideOutputs : config.hideInputs) ||
    (error && config.hideErrorDetails) ||
    v === undefined
  )
    return undefined;
  // Redact whole structured values before the display cap can split a secret.
  const safe = redactStructured(v);
  let value = typeof safe === "string" ? safe : JSON.stringify(safe);
  if (value === undefined) return undefined;
  for (const key of [config.credentials?.apiKey, config.credentials?.secretKey])
    if (key) value = value.split(key).join("[REDACTED]");
  return value.slice(0, 20000);
}
export function failedResult(value: unknown): boolean {
  if (typeof value === "string" && value.length <= 256 * 1024) {
    try { value = JSON.parse(value); } catch { return false; }
  }
  const result = record(value);
  const nested = record(result.ShellOutput ?? result.RunTerminalCommand);
  const exit = result.exit_code ?? nested.exit_code;
  return result.isError === true || result.is_error === true ||
    result.success === false || result.type === "error" ||
    result.status === "error" || result.status === "failed" ||
    (typeof exit === "number" && exit !== 0) || result.timed_out === true ||
    (result.signal != null && result.signal !== 0);
}
export interface GrokEvent {
  event: HookName;
  sessionId: string;
  promptId?: string;
  toolId?: string;
  toolName?: string;
  subagentId?: string;
  subagentType?: string;
  phase?: string;
  time: number;
  duration?: number;
  input?: string;
  output?: string;
  outcome?: TurnOutcome;
  // Routing/read context only. These fields are never span attributes.
  cwd?: string;
  transcriptPath?: string;
}
export function normalizeHook(
  value: unknown,
  config: ResolvedConfig,
): GrokEvent | undefined {
  const p = record(value),
    name = p.hookEventName ?? p.hook_event_name;
  const event = aliases[name] ?? name;
  const sessionId = id(p.sessionId ?? p.session_id);
  if (!HOOKS.includes(event) || !sessionId) return undefined;
  const isTool = [
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionDenied",
  ].includes(event);
  const failed = [
    "PostToolUseFailure",
    "PermissionDenied",
    "StopFailure",
  ].includes(event);
  const result = p.toolResult ?? p.tool_response;
  const outcome: TurnOutcome =
    event === "StopCancelled"
      ? "interrupted"
      : failed || failedResult(result)
        ? "error"
        : "success";
  const time = typeof p.timestamp === "string" ? Date.parse(p.timestamp) : NaN;
  const controlPath = (v: unknown): string | undefined =>
    typeof v === "string" &&
    v.startsWith("/") &&
    v.length <= 4096 &&
    !/[\x00-\x1f]/.test(v)
      ? v
      : undefined;
  return {
    event,
    sessionId,
    promptId: id(p.promptId ?? p.prompt_id),
    toolId: id(p.toolUseId ?? p.tool_use_id),
    toolName: id(p.toolName ?? p.tool_name),
    subagentId: id(p.subagentId),
    subagentType: id(p.subagentType),
    phase: id(p.phase),
    time: Number.isFinite(time) ? time : Date.now(),
    duration: count(p.durationMs ?? p.duration_ms),
    outcome,
    input: text(
      isTool
        ? (p.toolInput ?? p.tool_input)
        : event === "SubagentStart"
          ? p.description
          : p.prompt,
      config,
    ),
    output: text(
      failed
        ? (p.errorDetails ?? p.error ?? p.reason)
        : isTool
          ? result
          : (p.lastAssistantMessage ?? p.last_assistant_message),
      config,
      true,
      outcome === "error",
    ),
    cwd: controlPath(p.cwd),
    transcriptPath: controlPath(p.transcriptPath ?? p.transcript_path),
  };
}
/** turn_completed usage is turn-local. Never sum the cumulative _meta.totalTokens fields. */
export function turnUsage(value: unknown): UsageInput | undefined {
  const p = record(value);
  const input = count(p.inputTokens),
    output = count(p.outputTokens);
  if (input === undefined && output === undefined) return undefined;
  return {
    input,
    output,
    cacheRead: count(p.cachedReadTokens),
    cacheWrite: count(p.cacheCreationTokens),
    reasoning: count(p.reasoningTokens),
    cacheInInput: true,
    reasoningInOutput: true,
  };
  // cost_usd_ticks has no verified conversion contract. Intentionally never becomes dollars.
}
