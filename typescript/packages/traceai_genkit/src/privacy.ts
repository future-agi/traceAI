/**
 * Privacy for the exported copy of a Genkit span.
 *
 * Content follows the traceAI `TraceConfig` convention, like every traceAI instrumentor: Genkit's
 * inputs and outputs (prompts, model responses, tool arguments and results, flow input/output) are
 * exported by default, and the standard flags hide them: `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`,
 * `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT`,
 * `FI_HIDE_EMBEDDING_VECTORS`, or the same options on `traceConfig`. Error details follow
 * fi-core's tri-state `hideErrorDetails` / `FI_HIDE_ERROR_DETAILS` policy, derived from
 * input/output/message/text hiding when unset. Options override environment variables.
 *
 * Never exported, in either mode: Genkit's request context, stack traces, and local file paths
 * (rewritten to `[cwd]`, `~` or `[path]`). Error text additionally loses emails and this
 * machine's user and host names.
 */
import * as os from "os";
import {
  DEFAULT_HIDE_EMBEDDING_VECTORS,
  DEFAULT_HIDE_INPUTS,
  DEFAULT_HIDE_INPUT_MESSAGES,
  DEFAULT_HIDE_INPUT_TEXT,
  DEFAULT_HIDE_OUTPUTS,
  DEFAULT_HIDE_OUTPUT_MESSAGES,
  DEFAULT_HIDE_OUTPUT_TEXT,
  FI_HIDE_EMBEDDING_VECTORS,
  FI_HIDE_INPUTS,
  FI_HIDE_INPUT_MESSAGES,
  FI_HIDE_INPUT_TEXT,
  FI_HIDE_OUTPUTS,
  FI_HIDE_OUTPUT_MESSAGES,
  FI_HIDE_OUTPUT_TEXT,
  ERROR_DETAIL_ATTRIBUTE_KEYS,
  generateTraceConfig,
  HIDE_ERROR_DETAILS_IMPLIED_BY,
  REDACTED_VALUE,
  type TraceConfigOptions,
} from "@traceai/fi-core";

/** The fi-core TraceConfig flags this package reads, resolved. */
export interface ContentPolicy {
  hideInputs: boolean;
  hideOutputs: boolean;
  hideInputMessages: boolean;
  hideOutputMessages: boolean;
  hideInputText: boolean;
  hideOutputText: boolean;
  hideEmbeddingVectors: boolean;
  /** Optional for compatibility with existing policies; unset derives from content flags. */
  hideErrorDetails?: boolean;
}

/** Content captured, nothing hidden: fi-core's defaults (`DEFAULT_HIDE_* = false`). */
export const SHOW_ALL: Readonly<ContentPolicy> = Object.freeze({
  hideInputs: DEFAULT_HIDE_INPUTS,
  hideOutputs: DEFAULT_HIDE_OUTPUTS,
  hideInputMessages: DEFAULT_HIDE_INPUT_MESSAGES,
  hideOutputMessages: DEFAULT_HIDE_OUTPUT_MESSAGES,
  hideInputText: DEFAULT_HIDE_INPUT_TEXT,
  hideOutputText: DEFAULT_HIDE_OUTPUT_TEXT,
  hideEmbeddingVectors: DEFAULT_HIDE_EMBEDDING_VECTORS,
});

/** Every content flag on. */
export const HIDE_ALL: Readonly<ContentPolicy> = Object.freeze({
  hideInputs: true,
  hideOutputs: true,
  hideInputMessages: true,
  hideOutputMessages: true,
  hideInputText: true,
  hideOutputText: true,
  hideEmbeddingVectors: true,
});

/**
 * One fi-core boolean flag: option > env var > default, with fi-core's env rule
 * (`value.toLowerCase() === "true"`). Resolve these separately to preserve the legacy
 * `captureContent: false` fallback below options and environment variables.
 */
function flag(option: boolean | undefined, envKey: string, fallback: boolean): boolean {
  if (option !== undefined) return option;
  const value = typeof process !== "undefined" ? process.env?.[envKey] : undefined;
  if (value !== undefined) return value.toLowerCase() === "true";
  return fallback;
}

/**
 * Resolve the content flags. `fallback` replaces fi-core's defaults (used only for the removed
 * `captureContent: false` option, which must never export more than it asked for).
 */
export function resolveContentPolicy(
  traceConfig?: TraceConfigOptions,
  fallback: Partial<ContentPolicy> = {},
): ContentPolicy {
  const d = { ...SHOW_ALL, ...fallback };
  const content = {
    hideInputs: flag(traceConfig?.hideInputs, FI_HIDE_INPUTS, d.hideInputs),
    hideOutputs: flag(traceConfig?.hideOutputs, FI_HIDE_OUTPUTS, d.hideOutputs),
    hideInputMessages: flag(traceConfig?.hideInputMessages, FI_HIDE_INPUT_MESSAGES, d.hideInputMessages),
    hideOutputMessages: flag(traceConfig?.hideOutputMessages, FI_HIDE_OUTPUT_MESSAGES, d.hideOutputMessages),
    hideInputText: flag(traceConfig?.hideInputText, FI_HIDE_INPUT_TEXT, d.hideInputText),
    hideOutputText: flag(traceConfig?.hideOutputText, FI_HIDE_OUTPUT_TEXT, d.hideOutputText),
    hideEmbeddingVectors: flag(traceConfig?.hideEmbeddingVectors, FI_HIDE_EMBEDDING_VECTORS, d.hideEmbeddingVectors),
  };
  // Use fi-core's tri-state parser: option > valid env > derivation from resolved content flags.
  const { hideErrorDetails } = generateTraceConfig({ ...content, hideErrorDetails: traceConfig?.hideErrorDetails });
  return { ...content, hideErrorDetails };
}

/** True when free text written at run time (error messages) must not be exported. */
export function hidesFreeText(policy: ContentPolicy): boolean {
  return policy.hideErrorDetails ?? HIDE_ERROR_DETAILS_IMPLIED_BY.some((key) => policy[key]);
}

const ERROR_DETAIL_SUFFIXES = [...ERROR_DETAIL_ATTRIBUTE_KEYS].map((key) => `.${key}`).concat(".error_message");

/** The same exact and namespaced error-detail keys as fi-core's span/event masking. */
export function isErrorDetailAttribute(key: string): boolean {
  return ERROR_DETAIL_ATTRIBUTE_KEYS.has(key) || ERROR_DETAIL_SUFFIXES.some((suffix) => key.endsWith(suffix));
}

const MAX_REDACT_DEPTH = 64;

function redactTextFields(value: unknown, depth: number): unknown {
  if (depth > MAX_REDACT_DEPTH) return REDACTED_VALUE;
  if (Array.isArray(value)) return value.map((item) => redactTextFields(item, depth + 1));
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) {
      out[key] = (key === "text" || key === "reasoning") && typeof item === "string" ? REDACTED_VALUE : redactTextFields(item, depth + 1);
    }
    return out;
  }
  return value;
}

/**
 * A Genkit model request or response JSON with the text of every message part replaced by
 * fi-core's `__REDACTED__` (roles, tool requests, finish reason and usage kept), for
 * FI_HIDE_INPUT_TEXT / FI_HIDE_OUTPUT_TEXT. Genkit parts carry text as `{ text }` and model
 * reasoning as `{ reasoning }` (@genkit-ai/ai model-types.ts). Not JSON: `__REDACTED__`.
 */
export function redactMessageText(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  try {
    return JSON.stringify(redactTextFields(JSON.parse(value), 0));
  } catch {
    return REDACTED_VALUE;
  }
}

/**
 * A Genkit embedder response (`{ embeddings: [{ embedding: number[], metadata? }] }`) with every
 * vector replaced by `__REDACTED__`, for FI_HIDE_EMBEDDING_VECTORS. Not JSON: unchanged.
 */
export function redactEmbeddingVectors(value: unknown): unknown {
  if (typeof value !== "string") return value;
  try {
    const parsed = JSON.parse(value) as unknown;
    if (!parsed || typeof parsed !== "object" || !Array.isArray((parsed as { embeddings?: unknown }).embeddings)) return value;
    const response = parsed as { embeddings: unknown[] };
    return JSON.stringify({
      ...response,
      embeddings: response.embeddings.map((item) =>
        item && typeof item === "object" && "embedding" in item ? { ...item, embedding: REDACTED_VALUE } : item,
      ),
    });
  } catch {
    return value;
  }
}

// ---------------------------------------------------------------------------------------------
// Paths, stack traces and local identity.

export const PATH_REMOVED = "[path]";
export const CWD_TOKEN = "[cwd]";
export const STACK_TRACE_REMOVED = "[stack trace removed]";

/** Where the paths to rewrite come from. */
export interface ScrubContext {
  /** The process working directory. */
  cwd?: string;
  /** The user's home directory. */
  home?: string;
}

const PYTHON_TRACEBACK = /Traceback \(most recent call last\):[\s\S]*/g;
/**
 * A V8 stack: "\n    at fn (file:line:col)" frames, from the first frame on. Also matches frames
 * whose newline is JSON-escaped ("\\n    at ...") in text that is not valid JSON.
 */
const JS_STACK = /(?:(?:\r?\n|(?:\\+r)?\\+n)[ \t]+at [^\r\n\\"]+)+/g;
const STACK_HINT = /(?:\n|\\n)[ \t]+at /;
const FILE_URL = /file:\/\/[^\s"'`<>]+/g;
const WINDOWS_PATH = /(?<![\w])[A-Za-z]:\\[^\s"'`<>|*?]+/g;
const UNC_PATH = /(?<![\w\\])\\\\[^\s"'`<>|*?\\]+\\[^\s"'`<>|*?]+/g;
/** Absolute paths under the usual local roots; URL paths such as /v1/models and Genkit action paths are left alone. */
const LOCAL_PATH =
  /(?<![\w:/.~-])\/(?:Users|home|root|private|var|tmp|opt|usr|srv|mnt|Volumes|app|workspace|etc|run|proc|nix)\/[^\s"'`<>|*?,;)\]}]+/g;
const EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g;

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function replacePrefix(text: string, prefix: string | undefined, token: string): string {
  if (!prefix || prefix === "/" || !(prefix.startsWith("/") || /^[A-Za-z]:\\/.test(prefix))) return text;
  const trimmed = prefix.replace(/[\\/]+$/, "");
  if (trimmed.length < 2) return text;
  // The prefix itself, then a separator, the end, or a non-path character (not /a/proj2 for /a/proj).
  return text.replace(new RegExp(`${escapeRegExp(trimmed)}(?=[\\\\/]|$|[^\\w.-])`, "g"), token);
}

/** process.cwd() and os.homedir(), each only if readable. */
export function defaultScrubContext(): ScrubContext {
  const ctx: ScrubContext = {};
  try {
    ctx.cwd = process.cwd();
  } catch {
    // no cwd (deleted directory) or no process
  }
  try {
    ctx.home = os.homedir();
  } catch {
    // no home directory
  }
  return ctx;
}

/** Cheap pre-check: nothing to scrub without a path separator or a stack-trace shape. */
function mayNeedScrub(text: string): boolean {
  return /[/\\]/.test(text) || STACK_HINT.test(text) || text.includes("Traceback (most recent call last)");
}

/** Plain-text pass: stack traces out, local paths rewritten. Never inserts a raw newline. */
function scrubText(text: string, ctx: ScrubContext): string {
  let out = text;
  if (out.includes("Traceback (most recent call last)")) out = out.replace(PYTHON_TRACEBACK, STACK_TRACE_REMOVED);
  if (STACK_HINT.test(out)) out = out.replace(JS_STACK, ` ${STACK_TRACE_REMOVED}`);
  if (!/[/\\]/.test(out)) return out;
  out = out.replace(FILE_URL, PATH_REMOVED);
  // Longest first: the cwd usually sits under the home directory.
  const prefixes: Array<[string | undefined, string]> = [
    [ctx.cwd, CWD_TOKEN],
    [ctx.home, "~"],
  ];
  prefixes.sort((a, b) => (b[0]?.length ?? 0) - (a[0]?.length ?? 0));
  for (const [prefix, token] of prefixes) out = replacePrefix(out, prefix, token);
  out = out.replace(WINDOWS_PATH, PATH_REMOVED);
  out = out.replace(UNC_PATH, PATH_REMOVED);
  out = out.replace(LOCAL_PATH, PATH_REMOVED);
  return out;
}

const MAX_JSON_DEPTH = 4;

function looksLikeJson(text: string): boolean {
  const start = text.trimStart()[0];
  return start === "{" || start === "[";
}

/** Scrub every string inside parsed JSON (and JSON nested in those strings). */
function scrubJsonValue(value: unknown, ctx: ScrubContext, depth: number): unknown {
  if (typeof value === "string") return scrubString(value, ctx, depth + 1);
  if (Array.isArray(value)) return value.map((item) => scrubJsonValue(item, ctx, depth));
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) out[key] = scrubJsonValue(item, ctx, depth);
    return out;
  }
  return value;
}

function scrubString(text: string, ctx: ScrubContext, depth: number): string {
  if (!text || !mayNeedScrub(text)) return text;
  // Genkit serializes inputs, outputs and tool results to JSON, where a stack trace's newlines are
  // escaped. Parse, scrub each string, and re-serialize only when something changed.
  if (depth < MAX_JSON_DEPTH && looksLikeJson(text)) {
    try {
      const parsed = JSON.parse(text);
      const scrubbed = JSON.stringify(scrubJsonValue(parsed, ctx, depth));
      return scrubbed === JSON.stringify(parsed) ? text : scrubbed;
    } catch {
      // not JSON after all: fall through to the text pass (escaped newlines are handled there)
    }
  }
  return scrubText(text, ctx);
}

/**
 * Remove stack traces and rewrite local paths: the cwd becomes `[cwd]`, the home directory `~`,
 * any other absolute local path, file URL or Windows path `[path]`. Applied to every exported
 * string attribute, including JSON documents (inputs, outputs, tool results).
 */
export function scrubContent(text: string, ctx: ScrubContext = {}): string {
  return scrubString(text, ctx, 0);
}

let identity: string[] | undefined;

/** This machine's user name and host names (3+ characters, not "localhost"), longest first. */
function localIdentity(): string[] {
  if (identity) return identity;
  const out: string[] = [];
  try {
    const user = os.userInfo().username;
    if (user && user.length >= 3) out.push(user);
  } catch {
    // no user info (sandboxed runtime)
  }
  try {
    const host = os.hostname();
    for (const name of [host, host?.split(".")[0]]) {
      if (name && name.length >= 3 && name.toLowerCase() !== "localhost" && !out.includes(name)) out.push(name);
    }
  } catch {
    // no hostname
  }
  identity = out.sort((a, b) => b.length - a.length);
  return identity;
}

/** Error text as exported: `scrubContent`, then emails and this machine's user and host names. */
export function scrubErrorText(text: string, ctx: ScrubContext = {}): string {
  let out = scrubContent(text, ctx);
  out = out.replace(EMAIL, "<email>");
  for (const value of localIdentity()) out = out.split(value).join("<redacted>");
  return out;
}
