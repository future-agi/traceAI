/**
 * What never leaves the process, in either privacy mode: local file paths
 * (they carry the user name), stack traces, and, in error text, emails and
 * this machine's user and host names.
 *
 * Content (prompt, tool input/output, assistant text) is otherwise exported
 * as the agent saw it when capture is on. Paths inside it are rewritten:
 * the query's working directory becomes `[cwd]`, the home directory `~`, and
 * any other absolute local path `[path]`, so a Read of `<cwd>/src/a.ts` is
 * still readable as `[cwd]/src/a.ts`.
 */
import * as os from "os";

export const PATH_REMOVED = "[path]";
export const CWD_TOKEN = "[cwd]";
export const STACK_TRACE_REMOVED = "[stack trace removed]";

/** Where the paths to rewrite come from. */
export interface ScrubContext {
  /** The query's working directory (options.cwd, else process.cwd()). */
  cwd?: string;
  /** The user's home directory (os.homedir()). */
  home?: string;
}

const PYTHON_TRACEBACK = /Traceback \(most recent call last\):[\s\S]*/g;
/** A V8 stack: "\n    at fn (file:line:col)" frames, from the first frame on. */
const JS_STACK = /(?:\r?\n[ \t]+at [^\r\n]+)+/g;
const FILE_URL = /file:\/\/[^\s"'`<>]+/g;
const WINDOWS_PATH = /(?<![\w])[A-Za-z]:\\[^\s"'`<>|*?]+/g;
const UNC_PATH = /(?<![\w\\])\\\\[^\s"'`<>|*?\\]+\\[^\s"'`<>|*?]+/g;
/** Absolute paths under the usual local roots; API routes such as /v1/messages are left alone. */
const LOCAL_PATH =
  /(?<![\w:/.~-])\/(?:Users|home|root|private|var|tmp|opt|usr|srv|mnt|Volumes|app|workspace|etc|run|proc|nix)\/[^\s"'`<>|*?,;)\]}]+/g;
const EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g;

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function replacePrefix(text: string, prefix: string | undefined, token: string): string {
  if (!prefix || prefix.length < 2 || prefix === "/" || !(prefix.startsWith("/") || /^[A-Za-z]:\\/.test(prefix))) {
    return text;
  }
  const trimmed = prefix.replace(/[\\/]+$/, "");
  if (trimmed.length < 2) return text;
  // The prefix itself, then a separator or a non-path character (not /Users/a/proj2 for /Users/a/proj).
  return text.replace(new RegExp(`${escapeRegExp(trimmed)}(?=[\\\\/]|$|[^\\w.-])`, "g"), token);
}

/** Default context: process.cwd() and os.homedir(), each only if readable. */
export function defaultScrubContext(cwd?: unknown): ScrubContext {
  const ctx: ScrubContext = {};
  if (typeof cwd === "string" && cwd) {
    ctx.cwd = cwd;
  } else {
    try {
      ctx.cwd = process.cwd();
    } catch {
      // no cwd (deleted directory)
    }
  }
  try {
    ctx.home = os.homedir();
  } catch {
    // no home directory
  }
  return ctx;
}

/** Remove stack traces and rewrite local paths in exported content. */
export function scrubContent(text: string, ctx: ScrubContext = {}): string {
  if (!text) return text;
  let out = text;
  if (out.includes("Traceback (most recent call last)")) {
    out = out.replace(PYTHON_TRACEBACK, STACK_TRACE_REMOVED);
  }
  if (/\n[ \t]+at /.test(out)) {
    out = out.replace(JS_STACK, `\n${STACK_TRACE_REMOVED}`);
  }
  out = out.replace(FILE_URL, PATH_REMOVED);
  // Longest first: the cwd usually sits under the home directory.
  const prefixes: Array<[string | undefined, string]> = [
    [ctx.cwd, CWD_TOKEN],
    [ctx.home, "~"],
  ];
  prefixes.sort((a, b) => (b[0]?.length ?? 0) - (a[0]?.length ?? 0));
  for (const [prefix, token] of prefixes) {
    out = replacePrefix(out, prefix, token);
  }
  out = out.replace(WINDOWS_PATH, PATH_REMOVED);
  out = out.replace(UNC_PATH, PATH_REMOVED);
  out = out.replace(LOCAL_PATH, PATH_REMOVED);
  return out;
}

function localIdentity(): string[] {
  const out: string[] = [];
  try {
    const user = os.userInfo().username;
    if (user && user.length >= 3) out.push(user);
  } catch {
    // no user info (sandboxed runtime)
  }
  try {
    const host = os.hostname();
    if (host && host.length >= 3) {
      out.push(host);
      const short = host.split(".")[0];
      if (short && short.length >= 3 && short !== host) out.push(short);
    }
  } catch {
    // no hostname
  }
  return out.sort((a, b) => b.length - a.length);
}

/**
 * Error text as exported: `scrubContent`, then emails and this machine's user
 * and host names replaced. An SDK/CLI error such as "spawn /Users/<you>/...
 * ENOENT" keeps its meaning without the path.
 */
export function scrubErrorText(text: string, ctx: ScrubContext = {}): string {
  let out = scrubContent(text, ctx);
  out = out.replace(EMAIL, "<email>");
  for (const value of localIdentity()) {
    out = out.split(value).join("<redacted>");
  }
  return out;
}
