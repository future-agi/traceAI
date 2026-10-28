/**
 * Privacy for the exported copy of a VoltAgent span.
 *
 * Content follows the traceAI `TraceConfig` convention, like every traceAI instrumentor: prompts,
 * answers, tool arguments/results and other run content are exported by default, and the standard
 * flags hide them (`FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_MESSAGES`,
 * `FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT`, or the same options on
 * `traceConfig`). Precedence and parsing are fi-core's: option > env var > default, and only the
 * string "true" (any case) hides.
 *
 * Never exported, in either mode: credentials, stack traces, and local file paths (rewritten to
 * `[cwd]`, `~` or `[path]`); error text additionally loses emails and this machine's user and host
 * names.
 */
import * as os from "os";
import {
  DEFAULT_HIDE_INPUTS,
  DEFAULT_HIDE_INPUT_MESSAGES,
  DEFAULT_HIDE_INPUT_TEXT,
  DEFAULT_HIDE_OUTPUTS,
  DEFAULT_HIDE_OUTPUT_MESSAGES,
  DEFAULT_HIDE_OUTPUT_TEXT,
  FI_HIDE_INPUTS,
  FI_HIDE_INPUT_MESSAGES,
  FI_HIDE_INPUT_TEXT,
  FI_HIDE_OUTPUTS,
  FI_HIDE_OUTPUT_MESSAGES,
  FI_HIDE_OUTPUT_TEXT,
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
}

/** Content captured, nothing hidden: fi-core's defaults (`DEFAULT_HIDE_* = false`). */
export const SHOW_ALL: Readonly<ContentPolicy> = Object.freeze({
  hideInputs: DEFAULT_HIDE_INPUTS,
  hideOutputs: DEFAULT_HIDE_OUTPUTS,
  hideInputMessages: DEFAULT_HIDE_INPUT_MESSAGES,
  hideOutputMessages: DEFAULT_HIDE_OUTPUT_MESSAGES,
  hideInputText: DEFAULT_HIDE_INPUT_TEXT,
  hideOutputText: DEFAULT_HIDE_OUTPUT_TEXT,
});

/**
 * One fi-core boolean flag: option > env var > default, with fi-core's env rule
 * (`value.toLowerCase() === "true"`). Read here because fi-core's `generateTraceConfig` is not
 * exported and only reads the env vars when a config object is passed.
 */
function flag(option: boolean | undefined, envKey: string, fallback: boolean): boolean {
  if (option !== undefined) return option;
  const value = typeof process !== "undefined" ? process.env?.[envKey] : undefined;
  if (value !== undefined) return value.toLowerCase() === "true";
  return fallback;
}

export function resolveContentPolicy(traceConfig?: TraceConfigOptions): ContentPolicy {
  return {
    hideInputs: flag(traceConfig?.hideInputs, FI_HIDE_INPUTS, DEFAULT_HIDE_INPUTS),
    hideOutputs: flag(traceConfig?.hideOutputs, FI_HIDE_OUTPUTS, DEFAULT_HIDE_OUTPUTS),
    hideInputMessages: flag(traceConfig?.hideInputMessages, FI_HIDE_INPUT_MESSAGES, DEFAULT_HIDE_INPUT_MESSAGES),
    hideOutputMessages: flag(traceConfig?.hideOutputMessages, FI_HIDE_OUTPUT_MESSAGES, DEFAULT_HIDE_OUTPUT_MESSAGES),
    hideInputText: flag(traceConfig?.hideInputText, FI_HIDE_INPUT_TEXT, DEFAULT_HIDE_INPUT_TEXT),
    hideOutputText: flag(traceConfig?.hideOutputText, FI_HIDE_OUTPUT_TEXT, DEFAULT_HIDE_OUTPUT_TEXT),
  };
}

/** True when free text written at run time (error text, reasons) must not be exported. */
export function hidesFreeText(policy: ContentPolicy): boolean {
  return policy.hideInputs || policy.hideOutputs;
}

/**
 * Message-list JSON with every message's text replaced by fi-core's `__REDACTED__` (roles kept),
 * for FI_HIDE_INPUT_TEXT / FI_HIDE_OUTPUT_TEXT. VoltAgent writes `[{ role, content }]` model
 * messages and `[{ role, parts }]` UI messages. Anything else is dropped (undefined).
 */
export function redactMessageText(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  try {
    const messages = JSON.parse(value);
    if (!Array.isArray(messages)) return undefined;
    return JSON.stringify(
      messages.map((message) => {
        if (!message || typeof message !== "object") return REDACTED_VALUE;
        const copy: Record<string, unknown> = {};
        if ("role" in message) copy.role = (message as { role: unknown }).role;
        if ("content" in message) copy.content = REDACTED_VALUE;
        if ("parts" in message) copy.parts = REDACTED_VALUE;
        return copy;
      }),
    );
  } catch {
    return undefined;
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
/** Absolute paths under the usual local roots; URL paths such as /v1/chat/completions are left alone. */
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
  if (!text) return text;
  // VoltAgent serializes messages, tool results and state to JSON, where a stack trace's newlines
  // are escaped. Parse, scrub each string, and re-serialize only when something changed.
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
 * string value, including JSON documents (messages, tool results, state snapshots).
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
