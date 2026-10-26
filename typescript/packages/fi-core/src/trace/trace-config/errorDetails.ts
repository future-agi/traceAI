import {
  Attributes,
  SpanStatus,
  SpanStatusCode,
} from "@opentelemetry/api";

/**
 * Error-detail policy shared by {@link FISpan}.
 *
 * Always:
 * - an ERROR status keeps its description, but only the exception message, never a stack trace;
 * - spans that end in ERROR carry `error.type` (provider error code when the error has one,
 *   otherwise the error class name, `_OTHER` when neither is known);
 * - exception events keep `exception.type`.
 *
 * When `hideErrorDetails` is on:
 * - the ERROR status has no description;
 * - `exception.message`, `exception.stacktrace`, `error.message` and instrumentor keys ending in
 *   `.error.message` / `.error_message` / `.exception.message` / `.exception.stacktrace` are dropped
 *   from span attributes, exception events and other events (a denylist, as in the Python core's
 *   `strip_error_details`: `exception.type`, `exception.escaped` and other attributes are kept).
 */

export const ERROR_TYPE = "error.type";
export const ERROR_MESSAGE = "error.message";
export const EXCEPTION_EVENT_NAME = "exception";
export const EXCEPTION_TYPE = "exception.type";
export const EXCEPTION_MESSAGE = "exception.message";
export const EXCEPTION_STACKTRACE = "exception.stacktrace";
export const EXCEPTION_ESCAPED = "exception.escaped";
/** The OTel semantic-convention fallback for `error.type` when no better value is known */
export const ERROR_TYPE_OTHER = "_OTHER";

/**
 * Span / event attribute keys that carry error text (messages, stack traces, provider error bodies).
 * They are dropped when `hideErrorDetails` is on.
 */
export const ERROR_DETAIL_ATTRIBUTE_KEYS: ReadonlySet<string> = new Set([
  EXCEPTION_MESSAGE,
  EXCEPTION_STACKTRACE,
  ERROR_MESSAGE,
]);

/**
 * Instrumentor-specific keys of the same shape, e.g. `autogen.error.message` or
 * `autogen.tool.error_message`. Same list as the Python core.
 */
const ERROR_DETAIL_ATTRIBUTE_SUFFIXES: readonly string[] = [
  ".exception.message",
  ".exception.stacktrace",
  ".error.message",
  ".error_message",
];

/**
 * A provider / system error code that is safe to use as `error.type`: a short identifier such as
 * `model_not_found`, `ValidationException` or `ECONNREFUSED`, not prose, JSON or a quoted prompt.
 * Same pattern as the Python core.
 */
const ERROR_CODE_PATTERN = /^[A-Za-z][A-Za-z0-9_.:-]{0,63}$/;
/** Class / error names and codes that say nothing beyond "it is an error" */
const GENERIC_NAMES = new Set(["Error", "Object", "error"]);

function getStringProperty(value: unknown, key: string): string | undefined {
  if (value == null || (typeof value !== "object" && typeof value !== "function")) {
    return undefined;
  }
  try {
    const property = (value as Record<string, unknown>)[key];
    return typeof property === "string" && property.length > 0
      ? property
      : undefined;
  } catch {
    return undefined;
  }
}

function getProperty(value: unknown, key: string): unknown {
  if (value == null || (typeof value !== "object" && typeof value !== "function")) {
    return undefined;
  }
  try {
    return (value as Record<string, unknown>)[key];
  } catch {
    return undefined;
  }
}

/** The value when it is a usable error code (an identifier, not a generic word) */
function asErrorCode(value: unknown): string | undefined {
  return typeof value === "string" &&
    ERROR_CODE_PATTERN.test(value) &&
    !GENERIC_NAMES.has(value)
    ? value
    : undefined;
}

/** The provider / system error code carried by the error, when there is one */
function getProviderErrorCode(exception: unknown): string | undefined {
  // OpenAI-compatible SDKs (openai, groq, together, ...) and Node system errors.
  const code = asErrorCode(getProperty(exception, "code"));
  if (code != null) {
    return code;
  }
  // Anthropic: error = body = {"type": "error", "error": {"type": "not_found_error", ...}}.
  const body = getProperty(getProperty(exception, "error"), "error");
  for (const key of ["code", "type"]) {
    const bodyCode = asErrorCode(getProperty(body, key));
    if (bodyCode != null) {
      return bodyCode;
    }
  }
  // OpenAI APIError without a code: type = "invalid_request_error".
  return asErrorCode(getProperty(exception, "type"));
}

function getConstructorName(value: unknown): string | undefined {
  if (value == null || typeof value !== "object") {
    return undefined;
  }
  try {
    const name = (value as { constructor?: { name?: unknown } }).constructor
      ?.name;
    return typeof name === "string" && name.length > 0 ? name : undefined;
  } catch {
    return undefined;
  }
}

/**
 * The value for `error.type`: the provider / system error code when the error carries one
 * (OpenAI's `model_not_found`, Anthropic's `not_found_error`, Node's `ECONNREFUSED`), otherwise
 * the error class name (e.g. `NotFoundError`, `ValidationException`), otherwise `_OTHER`.
 * JavaScript has no module-qualified class names, so the class name is the most specific type.
 */
export function getErrorType(exception: unknown): string {
  const code = getProviderErrorCode(exception);
  if (code != null) {
    return code;
  }
  const constructorName = getConstructorName(exception);
  if (constructorName != null && !GENERIC_NAMES.has(constructorName)) {
    return constructorName;
  }
  const name = getStringProperty(exception, "name");
  if (name != null && ERROR_CODE_PATTERN.test(name)) {
    return name;
  }
  return ERROR_TYPE_OTHER;
}

/**
 * The value for `exception.type`, the same whether error details are hidden or not.
 *
 * Like the OpenTelemetry JS SDK, a string `code` that is an identifier comes first (Node's
 * `ECONNREFUSED`, OpenAI's `model_not_found`). Otherwise it is the error name / class, as in
 * Python, where `exception.type` is the exception class: a numeric code such as an HTTP `400`
 * (which the SDK would record as `"400"`) is not a type. `_OTHER` when nothing is known.
 */
export function getExceptionType(exception: unknown): string {
  const code = getProperty(exception, "code");
  if (typeof code === "string" && ERROR_CODE_PATTERN.test(code)) {
    return code;
  }
  const name = getStringProperty(exception, "name");
  if (name != null && ERROR_CODE_PATTERN.test(name)) {
    return name;
  }
  const constructorName = getConstructorName(exception);
  if (constructorName != null && !GENERIC_NAMES.has(constructorName)) {
    return constructorName;
  }
  return ERROR_TYPE_OTHER;
}

/**
 * The exception message only (never the stack trace), or undefined when there is none.
 */
export function getErrorMessage(exception: unknown): string | undefined {
  if (typeof exception === "string") {
    return exception.length > 0 ? exception : undefined;
  }
  return getStringProperty(exception, "message");
}

/**
 * A JavaScript stack frame line, e.g. "    at run (/srv/app/index.js:10:5)", "    at /srv/app.js:3:1",
 * "    at new Foo (<anonymous>)" or "    at Array.map (native)". The location suffix keeps prose
 * such as "  at least one item is required" from matching.
 */
const JS_STACK_FRAME = /^\s+at\s.*(?::\d+:\d+\)?|\(native\)|<anonymous>\)?)\s*$/;
/**
 * The offset of the first stack frame line (see {@link JS_STACK_FRAME}), or -1. Walks the lines
 * with indexOf, so no array of lines is built and no line is looked at twice.
 */
function indexOfFirstFrameLine(text: string): number {
  let lineStart = 0;
  while (lineStart <= text.length) {
    let lineEnd = text.indexOf("\n", lineStart);
    if (lineEnd === -1) {
      lineEnd = text.length;
    }
    // A frame line starts with whitespace; the cheap check skips the regex for all other lines
    if (lineEnd > lineStart && isWhitespaceCode(text.charCodeAt(lineStart))) {
      if (JS_STACK_FRAME.test(text.slice(lineStart, lineEnd))) {
        return lineStart;
      }
    }
    lineStart = lineEnd + 1;
  }
  return -1;
}

/** The header of a Python traceback relayed inside an error message */
const PYTHON_TRACEBACK = "Traceback (most recent call last):";
/** The lines Python prints between chained tracebacks */
const PYTHON_CHAINING_LINES: ReadonlySet<string> = new Set([
  "During handling of the above exception, another exception occurred:",
  "The above exception was the direct cause of the following exception:",
]);
/** An error name on its own, followed by ":" ("Error:"), left in front of a relayed traceback */
const BARE_ERROR_NAME = /^[A-Za-z_$][\w$.]*:$/;

/** "  + Exception Group" at the end of the text removed, then trimmed (linear, no regex) */
function dropExceptionGroupSuffix(text: string): string {
  const trimmed = text.trimEnd();
  const suffix = "Exception Group";
  if (!trimmed.endsWith(suffix)) {
    return text.trim();
  }
  let end = trimmed.length - suffix.length;
  while (end > 0 && /[\s+|]/.test(trimmed[end - 1])) {
    end--;
  }
  return trimmed.slice(0, end).trim();
}

/**
 * Removes relayed Python tracebacks: the "Traceback (most recent call last):" header and the
 * indented frame / source lines under it. The final "Type: message" line is kept, since it is
 * the error message.
 */
function stripPythonTracebacks(lines: string[]): { lines: string[]; stripped: boolean } {
  const kept: string[] = [];
  let inTraceback = false;
  let stripped = false;
  for (const line of lines) {
    const header = line.indexOf(PYTHON_TRACEBACK);
    if (header !== -1) {
      // e.g. "Error: Traceback (most recent call last):" when the traceback is an Error's message,
      // or "  + Exception Group Traceback (most recent call last):"
      const before = dropExceptionGroupSuffix(line.slice(0, header));
      if (before.length > 0 && !BARE_ERROR_NAME.test(before)) {
        kept.push(before);
      }
      inTraceback = true;
      stripped = true;
      continue;
    }
    if (inTraceback) {
      // Frames ("  File ...") and source / caret lines are indented; the exception line is not.
      if (/^\s/.test(line) || line.trim().length === 0) {
        continue;
      }
      inTraceback = false;
    }
    if (stripped && PYTHON_CHAINING_LINES.has(line.trim())) {
      continue;
    }
    kept.push(line);
  }
  return { lines: kept, stripped };
}

/** Same characters as the regex `\s` and `String.prototype.trim` */
function isWhitespaceCode(code: number): boolean {
  return (
    (code >= 9 && code <= 13) ||
    code === 32 ||
    code === 160 ||
    code === 0x1680 ||
    (code >= 0x2000 && code <= 0x200a) ||
    code === 0x2028 ||
    code === 0x2029 ||
    code === 0x202f ||
    code === 0x205f ||
    code === 0x3000 ||
    code === 0xfeff
  );
}

function isNameStart(code: number): boolean {
  return (
    (code >= 65 && code <= 90) || (code >= 97 && code <= 122) || code === 95 || code === 36
  );
}

function isNamePart(code: number): boolean {
  return isNameStart(code) || (code >= 48 && code <= 57) || code === 46;
}

/**
 * LangChain's `run.error` is `message + "\n\n" + stack`, and a stack opens with
 * `"<Name>: <message>"`. When the text is the message followed by that header, returns the
 * message, so it is not repeated.
 *
 * Linear in the text size: every index only moves forward, no tail is sliced or matched by a
 * regex per separator, and the message is compared only when its length can fit the tail.
 */
function dropRepeatedStackHeader(text: string): string {
  const length = text.length;
  let separator = text.indexOf("\n\n");
  if (separator === -1) {
    return text;
  }
  // The text trimmed: [start, end)
  let start = 0;
  while (start < length && isWhitespaceCode(text.charCodeAt(start))) {
    start++;
  }
  let end = length;
  while (end > start && isWhitespaceCode(text.charCodeAt(end - 1))) {
    end--;
  }
  let scanned = start; // text[start, scanned) was scanned for the last non-whitespace
  let messageEnd = start; // end of the trimmed text before the separator
  let tail = 0; // the first non-whitespace at or after the last separator + 2
  while (separator !== -1) {
    for (; scanned < separator; scanned++) {
      if (!isWhitespaceCode(text.charCodeAt(scanned))) {
        messageEnd = scanned + 1;
      }
    }
    const messageLength = messageEnd - start;
    if (messageLength > 0) {
      tail = Math.max(tail, separator + 2);
      while (tail < end && isWhitespaceCode(text.charCodeAt(tail))) {
        tail++;
      }
      // rest === message
      if (tail + messageLength === end && text.startsWith(text.slice(start, messageEnd), tail)) {
        return text.slice(start, messageEnd);
      }
      // rest is "<Name>: <message>"
      if (tail < end && isNameStart(text.charCodeAt(tail))) {
        let cursor = tail + 1;
        while (cursor < end && isNamePart(text.charCodeAt(cursor))) {
          cursor++;
        }
        if (text.charCodeAt(cursor) === 58 && text.charCodeAt(cursor + 1) === 32) {
          cursor += 2;
          while (cursor < end && isWhitespaceCode(text.charCodeAt(cursor))) {
            cursor++;
          }
          if (
            cursor + messageLength === end &&
            text.startsWith(text.slice(start, messageEnd), cursor)
          ) {
            return text.slice(start, messageEnd);
          }
        }
      }
    }
    separator = text.indexOf("\n\n", separator + 2);
  }
  return text;
}

/**
 * Removes a stack trace from a status description, keeping the whole message.
 *
 * Handles a bare `error.stack` ("Error: msg\n    at ..."; the message can span several lines),
 * LangChain's `run.error` ("msg\n\nError: msg\n    at ...") and relayed Python tracebacks, whose
 * final "Type: message" line is kept. Text without a stack is returned unchanged.
 */
export function stripStackTrace(description: string): string {
  // A JS stack: everything from the first frame line on is the stack.
  const frame = indexOfFirstFrameLine(description);
  let stripped = frame !== -1;
  let text = frame === -1 ? description : description.slice(0, Math.max(frame - 1, 0));
  if (text.includes(PYTHON_TRACEBACK)) {
    // Runs of blank lines end up as one blank line anyway; collapsing them first keeps a message
    // of a million newlines from becoming a million-element array.
    const python = stripPythonTracebacks(text.replace(/\n{3,}/g, "\n\n").split("\n"));
    stripped = stripped || python.stripped;
    text = python.lines.join("\n");
  }
  if (!stripped) {
    return description;
  }
  return dropRepeatedStackHeader(text.replace(/\n{3,}/g, "\n\n").trim());
}

/**
 * Reduces an ERROR description to the exception message. `String(error)` and many callers build
 * `"<Name>: <message>"`; the type already lives in `error.type` and `exception.type`.
 */
function stripErrorName(description: string, exception: unknown): string {
  const message = getErrorMessage(exception);
  if (message == null || description === message) {
    return description;
  }
  const names = [
    getStringProperty(exception, "name"),
    getConstructorName(exception),
  ];
  for (const name of names) {
    if (name != null && description === `${name}: ${message}`) {
      return message;
    }
  }
  return description;
}

/**
 * Applies the error-detail policy to a span status.
 * @param exception - the exception last recorded on the span, if any
 */
export function sanitizeStatus(
  status: SpanStatus,
  hideErrorDetails: boolean,
  exception?: unknown,
): SpanStatus {
  if (status.message == null) {
    return status;
  }
  if (hideErrorDetails) {
    return { code: status.code };
  }
  if (status.code !== SpanStatusCode.ERROR || typeof status.message !== "string") {
    return status;
  }
  let message = stripStackTrace(status.message);
  if (exception != null) {
    message = stripErrorName(message, exception);
  }
  return message.length > 0
    ? { code: status.code, message }
    : { code: status.code };
}

/**
 * True when the attribute carries error text that must be dropped when `hideErrorDetails` is on.
 */
export function isErrorDetailAttribute(key: string): boolean {
  return (
    ERROR_DETAIL_ATTRIBUTE_KEYS.has(key) ||
    ERROR_DETAIL_ATTRIBUTE_SUFFIXES.some((suffix) => key.endsWith(suffix))
  );
}

/**
 * Drops the error detail attributes (see {@link isErrorDetailAttribute}) from event attributes.
 */
export function stripErrorDetailAttributes(
  attributes: Attributes | undefined,
): Attributes | undefined {
  if (attributes == null) {
    return attributes;
  }
  const kept: Attributes = {};
  for (const [key, value] of Object.entries(attributes)) {
    if (!isErrorDetailAttribute(key)) {
      kept[key] = value;
    }
  }
  return kept;
}

/**
 * The attributes of the "exception" event for {@link FISpan.recordException}: the same keys the
 * OpenTelemetry SDK records (`exception.type`, `exception.message`, `exception.stacktrace`), with
 * `exception.type` from {@link getExceptionType} so it is the same in both modes. With
 * `hideErrorDetails` on, the message and stack trace are left out.
 */
export function getExceptionEventAttributes(
  exception: unknown,
  hideErrorDetails: boolean,
): Attributes {
  const attributes: Attributes = {};
  if (exception == null) {
    return attributes;
  }
  if (typeof exception === "string") {
    // A thrown string has no type; the SDK records the message only. With the message hidden,
    // `_OTHER` keeps the event (OTel needs a type or a message on it).
    if (hideErrorDetails) {
      attributes[EXCEPTION_TYPE] = ERROR_TYPE_OTHER;
    } else if (exception.length > 0) {
      attributes[EXCEPTION_MESSAGE] = exception;
    }
    return attributes;
  }
  attributes[EXCEPTION_TYPE] = getExceptionType(exception);
  if (!hideErrorDetails) {
    const message = getStringProperty(exception, "message");
    if (message != null) {
      attributes[EXCEPTION_MESSAGE] = message;
    }
    const stack = getStringProperty(exception, "stack");
    if (stack != null) {
      attributes[EXCEPTION_STACKTRACE] = stack;
    }
  }
  return attributes;
}
