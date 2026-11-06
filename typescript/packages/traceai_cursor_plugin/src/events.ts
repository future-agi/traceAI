import {
  redact,
  redactStructured,
  type ResolvedConfig,
  type UsageInput,
} from "@traceai/agent-core/hook-state";

export const VERSION = "0.1.0"; // x-release-please-version
export const FIXTURE_HOST_VERSION = "3.24.18";
/** Qualified 3.x hook schema, observed on 3.24.18 and 3.26.13. */
export const supportedHostVersion = (v: unknown): v is string =>
  typeof v === "string" &&
  // IDE 3.x line, or the Cursor CLI's dated build id (observed 2026.11.05-86047df); same hook schema.
  (/^3\.\d+\.\d+$/.test(v) || /^20\d{2}\.\d{2}\.\d{2}-[0-9a-f]{7,40}$/.test(v));
export const EVENTS = [
  "beforeSubmitPrompt",
  "preToolUse",
  "postToolUse",
  "postToolUseFailure",
  "afterAgentResponse",
  "subagentStart",
  "subagentStop",
  "preCompact",
  "stop",
  "sessionEnd",
] as const;
export type EventName = (typeof EVENTS)[number];
export type Data = Record<string, unknown>;
export const object = (v: unknown): Data | undefined =>
  v !== null && typeof v === "object" && !Array.isArray(v)
    ? (v as Data)
    : undefined;
export const text = (v: unknown): string | undefined =>
  typeof v === "string" ? v : undefined;
// Tool ids in real 3.24.18 fixtures contain a newline. Preserve the full identity; never trim or split aliases.
export function sourceId(v: unknown): string | undefined {
  return typeof v === "string" &&
    v.length > 0 &&
    v.length <= 1024 &&
    !v.includes("\0")
    ? v
    : undefined;
}
/** Same failure signals for hook outputs and transcript tool-result envelopes. */
export function toolResultFailed(value: unknown, depth = 0): boolean {
  // Fail closed on uninspectable nesting, before error details can enter context.
  if (depth > 16) return true;
  if (typeof value === "string") {
    try {
      return toolResultFailed(JSON.parse(value), depth + 1);
    } catch {
      return false;
    }
  }
  if (Array.isArray(value))
    return value.some((item) => toolResultFailed(item, depth + 1));
  const result = object(value);
  return (
    !!result &&
    (result.success === false ||
      result.isError === true ||
      result.is_error === true ||
      (typeof result.exitCode === "number" && result.exitCode !== 0) ||
      toolResultFailed(result.content, depth + 1) ||
      (result.type === "text" && toolResultFailed(result.text, depth + 1)))
  );
}
export function filtered(
  v: unknown,
  config: ResolvedConfig,
  output = false,
  error = false,
): string | undefined {
  if (
    !config.captureContent ||
    (output ? config.hideOutputs : config.hideInputs) ||
    (error && config.hideErrorDetails)
  )
    return undefined;
  const clean = redactStructured(v);
  let value =
    typeof clean === "string"
      ? clean
      : clean === undefined
        ? undefined
        : JSON.stringify(clean);
  if (value === undefined) return undefined;
  for (const secret of [
    config.credentials?.apiKey,
    config.credentials?.secretKey,
  ])
    if (secret) value = value.split(secret).join("[REDACTED]");
  return redact(value).slice(0, 20000);
}
export interface CursorEvent {
  event: EventName;
  gap?: string;
  conversation: string;
  generation?: string;
  toolId?: string;
  parentToolId?: string;
  subagentId?: string;
  tool?: string;
  input?: string;
  output?: string;
  model?: string;
  outcome?: "success" | "error" | "interrupted" | "incomplete";
  usage?: UsageInput;
  transcript?: string;
  childTranscript?: string;
  projectDir?: string;
  receivedAt: number;
  durationMs?: number;
}
const toolNames = new Set([
  "Shell",
  "Read",
  "Write",
  "Edit",
  "Grep",
  "Glob",
  "Task",
  "Delete",
  "MCP",
]);
function usage(p: Data): UsageInput | undefined {
  const pairs = [
    ["input_tokens", "input"],
    ["output_tokens", "output"],
    ["cache_read_tokens", "cacheRead"],
    ["cache_write_tokens", "cacheWrite"],
  ] as const;
  const result: UsageInput = { cacheInInput: true, reasoningInOutput: true };
  let found = false;
  for (const [from, to] of pairs)
    if (
      typeof p[from] === "number" &&
      Number.isSafeInteger(p[from]) &&
      (p[from] as number) >= 0
    ) {
      result[to] = p[from] as number;
      found = true;
    }
  // These are turn totals, not individual request usage or inferred cost.
  return found ? result : undefined;
}
export function normalizeEvent(
  payload: unknown,
  expected: string,
  config: ResolvedConfig,
  profile: "ide" | "cloud" = "ide",
): CursorEvent {
  const p = object(payload);
  if (!p || (profile === "ide" ? !supportedHostVersion(p.cursor_version)
    : typeof p.cursor_version !== "string" || !/^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9.-]+)?$/.test(p.cursor_version)))
    throw new Error("UNKNOWN_CURSOR_VERSION");
  if (Array.isArray(p.workspace_roots) && p.workspace_roots.length > 1)
    throw new Error("AMBIGUOUS_PROJECT_SCOPE");
  if (!EVENTS.includes(expected as EventName) || p.hook_event_name !== expected)
    throw new Error("INVALID_CURSOR_EVENT");
  const conversation = sourceId(p.conversation_id),
    generation = sourceId(p.generation_id);
  if (
    !conversation ||
    conversation.length > 256 ||
    (generation && generation.length > 256) ||
    (!generation && expected !== "sessionEnd")
  )
    throw new Error("MISSING_SOURCE_ID");
  const status = expected === "sessionEnd" ? p.reason : p.status;
  const failure = expected === "postToolUseFailure" || status === "error";
  const model = p.model_id ?? p.model;
  const result: CursorEvent = {
    event: expected as EventName,
    conversation,
    generation,
    toolId: sourceId(p.tool_use_id),
    parentToolId: sourceId(p.parent_tool_use_id),
    subagentId: sourceId(p.subagent_id),
    tool: profile === "cloud" && typeof p.tool_name === "string" && p.tool_name.startsWith("MCP:") ? "MCP" : toolNames.has(p.tool_name as string)
      ? (p.tool_name as string)
      : "custom",
    model:
      typeof model === "string" && /^[a-zA-Z0-9_.:/-]{1,128}$/.test(model)
        ? model
        : undefined,
    outcome:
      p.is_interrupt === true || status === "aborted"
        ? "interrupted"
        : failure
          ? "error"
          : status === "completed"
            ? "success"
            : undefined,
    receivedAt: Date.now(),
    durationMs:
      typeof p.duration === "number" &&
      Number.isFinite(p.duration) &&
      p.duration >= 0 &&
      p.duration <= 86400000
        ? p.duration
        : undefined,
  };
  // Additive host fields (including sessionEnd.model_params) are compatible.
  // A changed shape keeps the identity/boundary, not unvalidated content.
  const invalidShape =
    (expected === "beforeSubmitPrompt" && typeof p.prompt !== "string") ||
    (["preToolUse", "postToolUse", "postToolUseFailure"].includes(expected) &&
      (!result.toolId ||
        (p.tool_name !== undefined && typeof p.tool_name !== "string"))) ||
    (expected === "afterAgentResponse" &&
      p.text !== undefined &&
      typeof p.text !== "string") ||
    (expected === "stop" &&
      !["completed", "error", "aborted"].includes(p.status as string)) ||
    [
      "input_tokens",
      "output_tokens",
      "cache_read_tokens",
      "cache_write_tokens",
    ].some(
      (k) =>
        p[k] !== undefined &&
        (!Number.isSafeInteger(p[k]) || (p[k] as number) < 0),
    );
  if (invalidShape) {
    result.gap = "UNKNOWN_CURSOR_SHAPE";
    return result;
  }
  if (expected === "beforeSubmitPrompt")
    result.input = filtered(p.prompt, config);
  if (
    expected === "preToolUse" ||
    expected === "postToolUse" ||
    expected === "postToolUseFailure"
  ) {
    // A success hook can still contain a failed shell command result.
    const failed = failure || toolResultFailed(p.tool_output);
    result.input = filtered(p.tool_input, config);
    result.output = filtered(
      failure ? p.error_message : p.tool_output,
      config,
      true,
      failed,
    );
    if (!result.toolId) throw new Error("MISSING_TOOL_ID");
    if (failed) result.outcome = "error";
  }
  if (expected === "afterAgentResponse")
    result.output = filtered(p.text, config, true);
  if (expected === "subagentStart" || expected === "subagentStop") {
    result.input = filtered(p.task, config);
    result.output = filtered(p.summary, config, true, failure);
  }
  if (profile === "ide" && (expected === "stop" || expected === "afterAgentResponse"))
    result.usage = usage(p);
  if (profile === "cloud") {
    let output = object(p.tool_output);
    if (!output && typeof p.tool_output === "string") {
      try { output = object(JSON.parse(p.tool_output)); } catch { /* opaque text */ }
    }
    if (output?.redacted === true) result.output = undefined;
  }
  // Paths are local control data only. They are never span attributes or persistent event journals.
  if (config.captureContent && profile === "ide") {
    result.transcript = text(p.transcript_path);
    result.childTranscript = text(p.agent_transcript_path);
  }
  if (Array.isArray(p.workspace_roots) && p.workspace_roots.length === 1)
    result.projectDir = text(p.workspace_roots[0]);
  return result;
}
