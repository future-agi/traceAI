import { redact, redactValue } from "@traceai/agent-core/privacy";
import type { ResolvedConfig } from "@traceai/agent-core/config";
export const EVENTS = [
  "SessionStart",
  "UserPromptSubmit",
  "PreModelRequest",
  "PostModelRequest",
  "PreToolUse",
  "PostToolUse",
  "SubagentStart",
  "SubagentStop",
  "PreCompact",
  "PostCompact",
  "Stop",
  "Interrupt",
  "SessionEnd",
] as const;
export interface Event {
  hook_event_name: (typeof EVENTS)[number];
  session_id: string;
  turn_id?: string;
  agent_id?: string;
  model?: string;
  cwd: string;
  transcript_path?: string;
  agent_transcript_path?: string;
  tool_name?: string;
  tool_use_id?: string;
  prompt?: string;
  tool_input?: string;
  tool_response?: string;
  last_assistant_message?: string;
  status?: string;
  error?: string;
  reason?: string;
  failed?: boolean;
  token_usage?: Record<string, number>;
  receivedAt: number;
  gap?: boolean;
  /** Content eviction is not loss of request/tool boundaries or usage. */
  content_truncated?: boolean;
  source_session_id?: string;
  source_turn_id?: string;
  /** Structural native dispatch IDs survive content opt-out; never prompts/arguments. */
  dispatch_target?: string;
  dispatch_turn?: string;
  spawned_agent_id?: string;
  parent_agent_id?: string;
  parent_turn_id?: string;
}
const id = (v: unknown): v is string =>
  typeof v === "string" && /^[a-zA-Z0-9_.:-]{1,256}$/.test(v);
/** Model labels are not hook IDs: custom providers use names such as openai/gpt-4.1-mini. */
export const modelName = (v: unknown): string | undefined =>
  typeof v === "string" &&
  /^[a-zA-Z0-9_.:/-]{1,128}$/.test(v) &&
  !v.includes("://")
    ? v
    : undefined;
/** Shell responses wrap JSON in status text, so they are not complete JSON strings.
 * Decode balanced object/array fragments before the shared recursive redactor;
 * otherwise an escaped assignment inside a non-secret field can be overlooked.
 * One linear scan, with the same bounded ingress as a native hook payload.
 */
function redactEmbeddedJson(text: string): string {
  if (text.length > 262144 || Buffer.byteLength(text) > 262144)
    return "[REDACTED_SIZE_LIMIT]";
  let start = -1,
    offset = 0,
    quoted = false,
    escaped = false,
    result = "";
  const stack: string[] = [];
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (start < 0) {
      if (char !== "{" && char !== "[") continue;
      start = i;
    }
    if (quoted) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') quoted = false;
      continue;
    }
    if (char === '"') {
      quoted = true;
      continue;
    }
    if (char === "{" || char === "[") {
      stack.push(char);
      if (stack.length > 32) return "[REDACTED_DEPTH_LIMIT]";
    } else if (char === "}" || char === "]") {
      if (stack.pop() !== (char === "}" ? "{" : "[")) {
        stack.length = 0;
        start = -1;
        continue;
      }
      if (!stack.length) {
        try {
          const parsed = JSON.parse(text.slice(start, i + 1));
          result +=
            text.slice(offset, start) + JSON.stringify(redactValue(parsed));
          offset = i + 1;
        } catch {
          // Code and other non-JSON fragments still receive free-text redaction.
        }
        start = -1;
      }
    }
  }
  return result + text.slice(offset);
}
export function cleanText(
  value: unknown,
  config: ResolvedConfig,
  output = false,
): string | undefined {
  if (
    !config.captureContent ||
    (output ? config.hideOutputs : config.hideInputs) ||
    value === undefined ||
    value === null
  )
    return undefined;
  let text =
    typeof value === "string"
      ? redactEmbeddedJson(value)
      : JSON.stringify(redactValue(value));
  for (const key of [config.credentials?.apiKey, config.credentials?.secretKey])
    if (key) text = text.split(key).join("[REDACTED]");
  // Redact before truncation so a quoted credential cut at the limit cannot leak.
  return redact(text).slice(0, 20000);
}
export function sanitizeEvent(
  raw: unknown,
  config: ResolvedConfig,
): Event | undefined {
  if (!raw || typeof raw !== "object") return;
  const r = raw as Record<string, unknown>;
  if (
    !EVENTS.includes(r.hook_event_name as any) ||
    !id(r.session_id) ||
    typeof r.cwd !== "string" ||
    r.cwd.length > 4096 ||
    !r.cwd.startsWith("/")
  )
    return;
  const event: Event = {
    hook_event_name: r.hook_event_name as Event["hook_event_name"],
    session_id: r.session_id,
    cwd: r.cwd,
    receivedAt: Date.now(),
  };
  for (const k of [
    "turn_id",
    "agent_id",
    "tool_name",
    "tool_use_id",
    "status",
    "reason",
  ] as const)
    if (id(r[k])) event[k] = r[k];
  event.model = modelName(r.model);
  for (const k of ["transcript_path", "agent_transcript_path"] as const)
    if (typeof r[k] === "string" && r[k].length < 4096 && r[k].startsWith("/"))
      event[k] = r[k];
  // Codex's hook display name concatenates namespace and tool without a dot.
  if (
    [
      "send_input",
      "multi_agent_v1.send_input",
      "multi_agent_v1send_input",
    ].includes(r.tool_name as string)
  ) {
    const object = (value: unknown): any => {
      try {
        return typeof value === "string" ? JSON.parse(value) : value;
      } catch {
        return undefined;
      }
    };
    const input = object(r.tool_input),
      response = object(r.tool_response);
    if (id(input?.target)) event.dispatch_target = input.target;
    if (id(response?.submission_id))
      event.dispatch_turn = response.submission_id;
  }
  if (
    r.hook_event_name === "PostToolUse" &&
    [
      "spawn_agent",
      "multi_agent_v1.spawn_agent",
      "multi_agent_v1spawn_agent",
    ].includes(r.tool_name as string)
  ) {
    try {
      const response =
        typeof r.tool_response === "string"
          ? JSON.parse(r.tool_response)
          : (r.tool_response as any);
      if (id(response?.agent_id)) event.spawned_agent_id = response.agent_id;
    } catch {
      /* Unknown tool response is not an ancestry edge. */
    }
  }
  event.prompt = cleanText(r.prompt, config);
  event.tool_input = cleanText(r.tool_input, config);
  event.failed = toolFailed(
    typeof r.tool_response === "string"
      ? r.tool_response
      : JSON.stringify(r.tool_response),
  );
  event.tool_response = cleanText(r.tool_response, config, true);
  event.last_assistant_message = cleanText(
    r.last_assistant_message,
    config,
    true,
  );
  if (event.failed && config.hideErrorDetails) event.tool_response = undefined;
  if (!config.hideErrorDetails) event.error = cleanText(r.error, config, true);
  if (r.token_usage && typeof r.token_usage === "object") {
    event.token_usage = {};
    for (const k of [
      "input_tokens",
      "output_tokens",
      "cached_input_tokens",
      "reasoning_output_tokens",
      "total_tokens",
    ]) {
      const n = (r.token_usage as Record<string, unknown>)[k];
      if (Number.isSafeInteger(n) && (n as number) >= 0)
        event.token_usage[k] = n as number;
    }
  }
  return event;
}
export function toolFailed(text: string | undefined): boolean {
  if (!text) return false;
  try {
    const v = JSON.parse(text);
    if (
      v.is_error === true ||
      v.success === false ||
      (typeof v.exit_code === "number" && v.exit_code !== 0)
    )
      return true;
  } catch {
    /* shell text */
  }
  return /(?:Process exited with code|Exit code:|exit_code["']?\s*:)\s*(-?[1-9]\d*)/i.test(
    text,
  );
}
