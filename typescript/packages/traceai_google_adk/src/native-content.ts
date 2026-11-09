import * as fiCore from "@traceai/fi-core";
import { REDACTED_VALUE, TraceConfig } from "@traceai/fi-core";

// Normalize spelling/case, but do not match token counts (e.g. max_tokens).
const CREDENTIAL_KEY = /(?:apikey|secretkey|secretaccesskey|accesskeyid|authorization|(?:api|access|refresh|id|auth|session|security|identity|bearer)token|clientsecret|password|passwd|privatekey|credentials|cookie|setcookie)$|^(?:token|secret|bearer)$/;
const AUTHORIZATION = /\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_=.~%-]+/gi;
const MAX_DEPTH = 64;

function redactPii(value: string, config: TraceConfig): string {
  return config.piiRedaction ? fiCore.redactPiiInString(value) : value;
}

// Keep only bounded machine-readable classifications from an error object.
// Error messages can be copied into tool responses and subsequent LLM history;
// dropping exception events alone does not protect these content attributes.
function errorSummary(value: unknown, config: TraceConfig, hideText: boolean): unknown {
  if (value === null || typeof value === "boolean") return value;
  if (typeof value !== "object" || Array.isArray(value)) return REDACTED_VALUE;
  return Object.fromEntries(Object.entries(value).map(([key, child]) => {
    const normalized = key.replace(/[^a-z0-9]/gi, "").toLowerCase();
    const classification = ["type", "name", "code", "status", "errorcode", "iserror"].includes(normalized);
    const safe = classification && (
      typeof child === "boolean"
      || typeof child === "number" && Number.isFinite(child)
      || typeof child === "string" && !hideText && /^[A-Za-z0-9_.-]{1,64}$/.test(child)
    );
    // Allowed classifications still obey PII and text redaction, including
    // numeric values that the shared scanner recognizes as sensitive.
    return [redactPii(key, config), safe ? sanitize(child, config, hideText, false) : REDACTED_VALUE];
  }));
}

function sanitize(value: unknown, config: TraceConfig, hideText: boolean, input: boolean, depth = 0): unknown {
  // Bound recursive JSON-in-JSON and fail closed on unexpectedly deep payloads.
  if (depth > MAX_DEPTH) return REDACTED_VALUE;
  if (typeof value === "string") {
    if (hideText) return REDACTED_VALUE;
    const trimmed = value.trim();
    if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
      try {
        return JSON.stringify(sanitize(JSON.parse(trimmed), config, hideText, input, depth + 1));
      } catch { return REDACTED_VALUE; }
    }
    return redactPii(value.replace(AUTHORIZATION, REDACTED_VALUE), config);
  }
  if (typeof value === "number") {
    const text = String(value);
    const redacted = redactPii(text, config);
    // A PII marker replaces the value, not the serialized numeric JSON token.
    // Preserve ordinary numbers as numbers; serialize markers as strings.
    return redacted === text ? value : redacted;
  }
  if (Array.isArray(value)) return value.map(item => sanitize(item, config, hideText, input, depth + 1));
  if (value !== null && typeof value === "object") {
    const envelope = value as Record<string, unknown>;
    // ADK model errors and MCP tool failures can carry details outside `error`.
    if (config.hideErrorDetails && (envelope.isError === true ||
      typeof envelope.errorCode === "string" && envelope.errorCode.length > 0 ||
      typeof envelope.errorCode === "number" && envelope.errorCode !== 0)) {
      return errorSummary(value, config, hideText);
    }
    return Object.fromEntries(Object.entries(value).map(([key, child]) => {
      const normalized = key.replace(/[^a-z0-9]/gi, "").toLowerCase();
      const hiddenImage = input && config.hideInputImages && (normalized === "inlinedata" || normalized === "filedata");
      const hiddenError = config.hideErrorDetails && ["errormessage", "exceptionmessage", "stack", "stacktrace", "traceback"].includes(normalized);
      if (CREDENTIAL_KEY.test(normalized) || hiddenImage || hiddenError) return [redactPii(key, config), REDACTED_VALUE];
      if (config.hideErrorDetails && ["error", "errors", "exception"].includes(normalized)) {
        return [redactPii(key, config), errorSummary(child, config, hideText)];
      }
      return [redactPii(key, config), sanitize(child, config, hideText, input, depth + 1)];
    }));
  }
  return value;
}

/** Native ADK JSON is not a gen_ai.*.messages attribute: mask before mapping it. */
export function nativeContent(value: unknown, input: boolean, config: TraceConfig): string | undefined {
  if (typeof value !== "string" || value === "{}") return undefined;
  // Native request/response envelopes contain message history, tool data and
  // system instructions. Hide the envelope for message-level opt-outs too;
  // retaining arbitrary config fields could re-expose the hidden messages.
  if (input ? config.hideInputs || config.hideInputMessages : config.hideOutputs || config.hideOutputMessages) {
    return REDACTED_VALUE;
  }
  try {
    const hideText = input ? config.hideInputText : config.hideOutputText;
    // Older published fi-core versions resolve the same policy but do not yet
    // expose its PII scanner. Keep them safe without requiring release ordering:
    // conservatively hide the payload until the shared scanner is available.
    if (config.piiRedaction && typeof fiCore.redactPiiInString !== "function") return REDACTED_VALUE;
    // Scan values and keys structurally, including JSON encoded in strings.
    // Scanning serialized JSON can produce unquoted markers for numeric PII.
    return JSON.stringify(sanitize(JSON.parse(value), config, hideText, input));
  } catch {
    // Truncated/malformed native JSON must never bypass credential filtering.
    return REDACTED_VALUE;
  }
}
