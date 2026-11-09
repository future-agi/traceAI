import { createHmac } from "node:crypto";
import {
  redact,
  type ResolvedConfig,
  type UsageInput,
  type TurnOutcome,
} from "@traceai/agent-core";

export const EVENTS = [
  "BeforeAgent",
  "BeforeModel",
  "AfterModel",
  "AfterTool",
  "AfterAgent",
  "PreCompress",
  "SessionEnd",
] as const;
export interface Event {
  hook_event_name: string;
  session_id: string;
  timestamp: string;
  prompt?: unknown;
  prompt_response?: unknown;
  llm_request?: unknown;
  llm_response?: unknown;
  tool_name?: unknown;
  tool_input?: unknown;
  tool_response?: unknown;
}
export interface Child {
  id: string;
  kind: "llm" | "tool" | "compaction";
  name?: string;
  model?: string;
  input?: string;
  output?: string;
  start: number;
  end?: number;
  observedResponse?: boolean;
  finished?: boolean;
  usage?: UsageInput;
  outcome?: TurnOutcome;
}
export interface TurnState {
  schema: 1;
  binding: string;
  session: string;
  turn: string;
  start: number;
  input?: string;
  output?: string;
  children: Child[];
  currentModel?: string;
  seen: string[];
  gap?: boolean;
}
export const object = (x: unknown): Record<string, any> =>
  x && typeof x === "object" && !Array.isArray(x)
    ? (x as Record<string, any>)
    : {};
export function opaque(config: ResolvedConfig, value: string): string {
  return createHmac("sha256", config.identityKey ?? "unconfigured")
    .update(value)
    .digest("hex");
}
export function binding(config: ResolvedConfig): string {
  return opaque(
    config,
    JSON.stringify([
      config.endpoint,
      config.projectName,
      config.generation,
      config.hideInputs,
      config.hideOutputs,
      config.hideErrorDetails,
    ]),
  );
}
/** Only allowlisted text enters recovery state. No raw hook envelopes or transcript paths. */
export function text(
  value: unknown,
  config: ResolvedConfig,
  output = false,
  error = false,
): string | undefined {
  if (
    (output ? config.hideOutputs : config.hideInputs) ||
    (error && config.hideErrorDetails) ||
    value === undefined
  )
    return;
  let result = (
    typeof value === "string" ? value : JSON.stringify(value)
  ).slice(0, 20000);
  for (const key of [config.credentials?.apiKey, config.credentials?.secretKey])
    if (key) result = result.split(key).join("[REDACTED]");
  return redact(result).slice(0, 20000);
}
const label = (v: unknown): string | undefined =>
  typeof v === "string" && /^[a-zA-Z0-9_.-]{1,128}$/.test(v) ? v : undefined;
export function eventTime(event: Event): number {
  const t = Date.parse(event.timestamp);
  if (!Number.isFinite(t)) throw new Error("INVALID_EVENT_TIME");
  return t;
}
export function validateEvent(value: unknown): Event {
  const x = object(value);
  if (
    !EVENTS.includes(x.hook_event_name) ||
    typeof x.session_id !== "string" ||
    !x.session_id ||
    x.session_id.length > 256 ||
    typeof x.timestamp !== "string"
  )
    throw new Error("INVALID_EVENT");
  eventTime(x as Event);
  return x as Event;
}
export function usage(value: unknown): UsageInput | undefined {
  const v = object(value);
  const n = (k: string): number | undefined =>
    typeof v[k] === "number" && Number.isSafeInteger(v[k]) && v[k] >= 0
      ? v[k]
      : undefined;
  const input = n("promptTokenCount"),
    candidates = n("candidatesTokenCount"),
    reasoning = n("thoughtsTokenCount"),
    cacheRead = n("cachedContentTokenCount");
  if ([input, candidates, reasoning, cacheRead].every((x) => x === undefined))
    return;
  // Gemini candidates excludes thoughts; preserve that inclusion when normalizing in core.
  return {
    input,
    output: candidates,
    reasoning,
    cacheRead,
    cacheInInput: true,
    reasoningInOutput: false,
  };
}
export function applyEvent(
  state: TurnState | undefined,
  event: Event,
  config: ResolvedConfig,
): TurnState | undefined {
  const now = eventTime(event);
  const fingerprint = opaque(config, JSON.stringify(event));
  if (state?.seen.includes(fingerprint)) return state;
  if (event.hook_event_name === "BeforeAgent") {
    return {
      schema: 1,
      binding: binding(config),
      session: opaque(config, event.session_id),
      turn: fingerprint,
      start: now,
      input: text(event.prompt, config),
      children: [],
      seen: [fingerprint],
    };
  }
  if (!state || state.binding !== binding(config)) return;
  state.seen.push(fingerprint);
  state.seen = state.seen.slice(-128);
  if (event.hook_event_name === "AfterAgent")
    state.output = text(event.prompt_response, config, true);
  if (state.children.length >= 96) {
    state.gap = true;
    return state;
  }
  if (event.hook_event_name === "BeforeModel") {
    const id = fingerprint;
    state.children.push({
      id,
      kind: "llm",
      start: now,
      model: label(object(event.llm_request).model),
    });
    state.currentModel = id;
  }
  if (event.hook_event_name === "AfterModel") {
    const child = state.children.find((c) => c.id === state.currentModel);
    if (!child) {
      state.gap = true;
      return state;
    }
    child.observedResponse = true;
    child.end = now;
    child.input = text(object(event.llm_request).messages, config);
    child.model = label(object(event.llm_request).model) ?? child.model;
    const response = object(event.llm_response);
    // Hook API filters non-text parts. Do not pretend these are the full multimodal messages.
    const first = object(
      Array.isArray(response.candidates) ? response.candidates[0] : undefined,
    );
    const parts = object(first.content).parts;
    const content = Array.isArray(parts)
      ? parts.filter((p) => typeof p === "string").join("")
      : "";
    const chunk = text(content, config, true);
    if (chunk !== undefined)
      child.output = ((child.output ?? "") + chunk).slice(0, 20000);
    child.finished ||=
      typeof first.finishReason === "string" && first.finishReason.length > 0;
    // Usage is a cumulative snapshot per request, not an additive amount per streamed chunk.
    child.usage = usage(response.usageMetadata) ?? child.usage;
  }
  if (event.hook_event_name === "AfterTool") {
    const response = object(event.tool_response),
      error =
        response.error !== undefined &&
        response.error !== null &&
        response.error !== false;
    state.children.push({
      id: fingerprint,
      kind: "tool",
      name: label(event.tool_name),
      input: text(event.tool_input, config),
      output: text(
        response.llmContent ?? response.returnDisplay,
        config,
        true,
        error,
      ),
      start: now,
      end: now,
      outcome: error ? "error" : "success",
    });
    // No source call id or start timestamp is exposed by this hook. Timing is an observation, not duration.
  }
  if (event.hook_event_name === "PreCompress")
    state.children.push({
      id: fingerprint,
      kind: "compaction",
      start: now,
      end: now,
    });
  return state;
}
