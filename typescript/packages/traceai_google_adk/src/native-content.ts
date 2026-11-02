import * as fiCore from "@traceai/fi-core";
import { REDACTED_VALUE, TraceConfig } from "@traceai/fi-core";

// Normalize spelling/case, but do not match token counts (e.g. max_tokens).
const CREDENTIAL_KEY = /(?:apikey|secretkey|authorization|accesstoken|refreshtoken|idtoken|authtoken|clientsecret|password|passwd|privatekey|credentials|cookie|setcookie)$|^(?:token|secret|bearer)$/;
const AUTHORIZATION = /\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_=.~%-]+/gi;
const MAX_DEPTH = 64;

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
    return value.replace(AUTHORIZATION, REDACTED_VALUE);
  }
  if (Array.isArray(value)) return value.map(item => sanitize(item, config, hideText, input, depth + 1));
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, child]) => {
      const normalized = key.replace(/[^a-z0-9]/gi, "").toLowerCase();
      const hiddenImage = input && config.hideInputImages && (normalized === "inlinedata" || normalized === "filedata");
      return [key, CREDENTIAL_KEY.test(normalized) || hiddenImage
        ? REDACTED_VALUE : sanitize(child, config, hideText, input, depth + 1)];
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
    const sanitized = JSON.stringify(sanitize(JSON.parse(value), config, hideText, input));
    if (!config.piiRedaction) return sanitized;
    // Older published fi-core versions resolve the same policy but do not yet
    // expose its PII scanner. Keep them safe without requiring release ordering:
    // conservatively hide the payload until the shared scanner is available.
    return typeof fiCore.redactPiiInString === "function"
      ? fiCore.redactPiiInString(sanitized) : REDACTED_VALUE;
  } catch {
    // Truncated/malformed native JSON must never bypass credential filtering.
    return REDACTED_VALUE;
  }
}
