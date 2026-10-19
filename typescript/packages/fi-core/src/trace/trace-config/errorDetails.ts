import {
  Attributes,
  AttributeValue,
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
 * - exception events keep only `exception.type` (and `exception.escaped`);
 * - `exception.message`, `exception.stacktrace`, `error.message` and instrumentor keys ending in
 *   `.error.message` / `.error_message` / `.exception.message` / `.exception.stacktrace` are dropped
 *   (span and event attributes).
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

/** The only exception-event attributes kept when `hideErrorDetails` is on */
const HIDDEN_EXCEPTION_EVENT_KEYS: ReadonlySet<string> = new Set([
  EXCEPTION_TYPE,
  EXCEPTION_ESCAPED,
]);

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
 * The `exception.type` the OpenTelemetry SDK records for the exception (`code`, else `name`), so
 * an exception event has the same type whether error details are hidden or not. Falls back to
 * {@link getErrorType} when that value is missing or is not an identifier.
 */
export function getExceptionType(exception: unknown): string {
  const code = getProperty(exception, "code");
  const sdkType =
    typeof code === "string" || typeof code === "number"
      ? String(code)
      : getStringProperty(exception, "name");
  return sdkType != null && ERROR_CODE_PATTERN.test(sdkType)
    ? sdkType
    : getErrorType(exception);
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
/** The start of a Python traceback relayed inside an error message */
const PYTHON_TRACEBACK = /^Traceback \(most recent call last\):/;
/** The "Name: message" header line that starts a JavaScript stack */
const STACK_HEADER = /^[A-Za-z_$][\w$.]*(?::\s|:?$)/;

/**
 * Removes a stack trace from a status description, keeping the message.
 *
 * Handles a bare `error.stack` ("Error: msg\n    at ..."), LangChain's `run.error`
 * ("msg\n\nError: msg\n    at ...") and relayed Python tracebacks.
 */
export function stripStackTrace(description: string): string {
  const lines = description.split("\n");
  const firstFrame = lines.findIndex(
    (line) => JS_STACK_FRAME.test(line) || PYTHON_TRACEBACK.test(line),
  );
  if (firstFrame === -1) {
    return description;
  }
  const kept = lines.slice(0, firstFrame);
  // Drop the "Name: message" header that opens a JS stack when the message is already above it.
  const header = kept.length > 0 ? kept[kept.length - 1] : undefined;
  if (header != null && STACK_HEADER.test(header)) {
    const before = kept.slice(0, -1).join("\n").trim();
    if (before.length > 0) {
      kept.pop();
    }
  }
  return kept.join("\n").trim();
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
 * Applies the error-detail policy to the attributes of an "exception" event.
 */
export function sanitizeExceptionEventAttributes(
  attributes: Attributes | undefined,
  hideErrorDetails: boolean,
): Attributes | undefined {
  if (!hideErrorDetails || attributes == null) {
    return attributes;
  }
  const kept: Attributes = {};
  for (const [key, value] of Object.entries(attributes)) {
    if (HIDDEN_EXCEPTION_EVENT_KEYS.has(key) && value != null) {
      kept[key] = value as AttributeValue;
    }
  }
  if (kept[EXCEPTION_TYPE] == null) {
    kept[EXCEPTION_TYPE] = ERROR_TYPE_OTHER;
  }
  return kept;
}

/**
 * The attributes of an "exception" event when `hideErrorDetails` is on: the type only.
 */
export function getHiddenExceptionEventAttributes(
  exception: unknown,
): Attributes {
  return { [EXCEPTION_TYPE]: getExceptionType(exception) };
}
