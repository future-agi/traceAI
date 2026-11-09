import { posix, win32 } from "node:path";
import {
  opaqueId,
  redact,
  type CoreAdapter,
  type ResolvedConfig,
} from "@traceai/agent-core";

export const GUI_QUALIFICATION_REQUIRED = "ANTIGRAVITY_GUI_QUALIFICATION_REQUIRED";
export const GUI_PROFILE_VERSION = "ide-v1";
export type GuiSurface = "ide" | "app";
export type GuiEventName =
  | "SessionStart"
  | "PreInvocation"
  | "PostInvocation"
  | "PostToolUse"
  | "Stop";
const events: readonly string[] = [
  "SessionStart", "PreInvocation", "PostInvocation", "PostToolUse", "Stop",
];
const maxPayloadBytes = 256 * 1024;
const maxContentChars = 20_000;

type Privacy = Pick<
  ResolvedConfig,
  "enabled" | "captureContent" | "hideInputs" | "hideOutputs" |
  "hideErrorDetails" | "credentials"
>;
export interface GuiParseOptions {
  /** Local configuration, never a hook-supplied root. No filesystem scan occurs. */
  home: string;
  surface: GuiSurface;
  identityKey: Uint8Array;
  privacy: Privacy;
}
/** Sanitized schema evidence, NOT a turn, model request, or exportable span.
 * Invocation numbers cannot establish a user-turn boundary. No transcript reader
 * or registration path is enabled until the actual GUI surface is qualified.
 */
export interface GuiObservation {
  profileVersion: typeof GUI_PROFILE_VERSION;
  surface: GuiSurface;
  event: GuiEventName;
  conversationKey: string;
  model?: string;
  invocationNum?: number;
  initialNumSteps?: number;
  stepIdx?: number;
  tool?: string;
  toolInput?: string;
  error?: string;
  failed?: boolean;
  executionNum?: number;
  fullyIdle?: boolean;
  outcome?: "success" | "error" | "interrupted" | "incomplete";
}
function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function counter(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 0;
}
function label(value: unknown, privacy: Privacy): string | undefined {
  if (typeof value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value))
    return undefined;
  if ([privacy.credentials?.apiKey, privacy.credentials?.secretKey].some(
    (key) => key && value.includes(key),
  )) return undefined;
  return value;
}
function text(value: unknown, privacy: Privacy): string | undefined {
  if (typeof value !== "string") return undefined;
  // Replace known keys before truncation, so a key crossing the cap cannot leak
  // an unredacted prefix into recovery state. The payload itself is bounded.
  let clean = value;
  for (const key of [privacy.credentials?.apiKey, privacy.credentials?.secretKey])
    if (key) clean = clean.split(key).join("[REDACTED]");
  return redact(clean).slice(0, maxContentChars);
}
/** Documented candidates only. This does not prove registration, ownership,
 * symlink safety, transcript availability, or that the application is installed.
 */
export function guiPathCandidates(home: string, surface: GuiSurface) {
  if (surface !== "ide" && surface !== "app") throw new Error("INVALID_GUI_SURFACE");
  const paths = /^[A-Za-z]:[\\/]/.test(home) ? win32 : posix;
  if (!paths.isAbsolute(home) || /[\x00-\x1f]/.test(home))
    throw new Error("INVALID_GUI_HOME");
  return {
    appData: paths.join(home, ".gemini", surface === "ide" ? "antigravity-ide" : "antigravity"),
    hooks: paths.join(home, ".gemini", "config", "hooks.json"),
    plugins: paths.join(home, ".gemini", "config", "plugins"),
  };
}
function matchesSurface(payload: Record<string, unknown>, options: GuiParseOptions): boolean {
  const conversation = payload.conversationId;
  if (typeof conversation !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(conversation))
    return false;
  const paths = /^[A-Za-z]:[\\/]/.test(options.home) ? win32 : posix;
  const root = paths.join(guiPathCandidates(options.home, options.surface).appData, "brain", conversation);
  const expected = paths.join(root, ".system_generated", "logs", "transcript.jsonl");
  // Exact lexical paths only: no basename-only/substring checks, expansion of ~,
  // traversal normalization, source-provided roots, or fallback from CLI paths.
  const normalizeSeparators = (value: string) => paths === win32 ? value.replace(/\//g, "\\") : value;
  if (typeof payload.transcriptPath !== "string" || normalizeSeparators(payload.transcriptPath) !== expected)
    return false;
  return payload.artifactDirectoryPath === undefined ||
    (typeof payload.artifactDirectoryPath === "string" && normalizeSeparators(payload.artifactDirectoryPath) === root);
}
/** Parse a single bounded JSON hook payload. No network, state, transcript reads,
 * global tracing provider, or raw-event persistence; malformed input fails open.
 * Undocumented fixture extras (prompt/output/usage/subagent fields) are dropped.
 */
export function parseGuiObservation(
  event: string,
  raw: string,
  options: GuiParseOptions,
): GuiObservation | undefined {
  try {
    if (!options.privacy.enabled || !events.includes(event) ||
        typeof raw !== "string" || raw.length > maxPayloadBytes ||
        Buffer.byteLength(raw) > maxPayloadBytes || options.identityKey.length < 32)
      return undefined;
    const payload: unknown = JSON.parse(raw);
    if (!object(payload) || !matchesSurface(payload, options)) return undefined;
    const privacy = options.privacy;
    const result: GuiObservation = {
      profileVersion: GUI_PROFILE_VERSION,
      surface: options.surface,
      event: event as GuiEventName,
      conversationKey: opaqueId(options.identityKey, `antigravity-${options.surface}:conversation`, payload.conversationId as string),
    };
    const model = label(payload.modelName, privacy);
    if (model) result.model = model;
    if (event === "PreInvocation" || event === "PostInvocation") {
      if (!counter(payload.invocationNum) || !counter(payload.initialNumSteps)) return undefined;
      result.invocationNum = payload.invocationNum;
      result.initialNumSteps = payload.initialNumSteps;
    }
    if (event === "PostToolUse") {
      if (!counter(payload.stepIdx) || !object(payload.toolCall)) return undefined;
      const tool = label(payload.toolCall.name, privacy);
      if (!tool) return undefined;
      if (payload.error !== undefined && typeof payload.error !== "string") return undefined;
      result.stepIdx = payload.stepIdx;
      result.tool = tool;
      result.failed = typeof payload.error === "string" && payload.error.length > 0;
      if (privacy.captureContent && !privacy.hideInputs && object(payload.toolCall.args))
        result.toolInput = text(JSON.stringify(payload.toolCall.args), privacy);
      if (privacy.captureContent && !privacy.hideErrorDetails && result.failed)
        result.error = text(payload.error, privacy);
    }
    if (event === "Stop") {
      if (!counter(payload.executionNum) || typeof payload.fullyIdle !== "boolean" ||
          typeof payload.terminationReason !== "string" ||
          (payload.error !== undefined && typeof payload.error !== "string")) return undefined;
      result.executionNum = payload.executionNum;
      result.fullyIdle = payload.fullyIdle;
      const reason = payload.terminationReason.toLowerCase();
      result.outcome = !payload.fullyIdle ? "incomplete" :
        payload.error || reason === "error" ? "error" :
        ["user_canceled", "user_cancelled", "cancelled", "canceled"].includes(reason) ? "interrupted" :
        ["model_stop", "no_tool_call"].includes(reason) ? "success" : "incomplete";
      if (privacy.captureContent && !privacy.hideErrorDetails)
        result.error = text(payload.error, privacy);
    }
    return result;
  } catch {
    return undefined;
  }
}

/** Not wired into installation: no amount of CLI/fixture success clears this
 * surface gate. Importing this adapter cannot launch a binary or alter hooks.
 */
export const antigravityGuiAdapter: CoreAdapter = Object.freeze({
  id: "antigravity-ide",
  version: "0.1.0",
  requiresWorker: false,
  async detect() { return { supported: false, reason: GUI_QUALIFICATION_REQUIRED }; },
  async install() { return { registered: false, activated: false, trust: "unknown" as const, detail: GUI_QUALIFICATION_REQUIRED }; },
  async inspect() { return { registered: false, activated: false, trust: "unknown" as const, detail: GUI_QUALIFICATION_REQUIRED }; },
  async uninstall() { return { registered: false, activated: false, trust: "unknown" as const, detail: "NO_OWNED_GUI_REGISTRATION" }; },
});
