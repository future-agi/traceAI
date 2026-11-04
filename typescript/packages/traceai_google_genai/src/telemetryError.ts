import { REDACTED_VALUE } from "@traceai/fi-core";

const sensitiveHeader =
  /^(?:authorization|proxy-authorization|(?:x-)?api[_-]?key|x-goog-api-key|x-secret-key|x-auth-token|x-access-token|cookie|set-cookie)$/i;
// null means capture failed: no error details are safe to emit in that case.
export type RequestCredentials = readonly string[] | null;

/** Snapshot effective credentials before invoking the SDK, without constructing
 * Headers (which can reject and quote the very secret we need to capture). */
export function requestCredentials(
  model: {
    apiKey?: unknown;
    _requestOptions?: { customHeaders?: unknown };
  },
  requestOptions: unknown,
): RequestCredentials {
  try {
    const credentials: string[] = [];
    const apiKey = model.apiKey;
    if (typeof apiKey === "string") credentials.push(apiKey);
    else if (apiKey != null) return null;
    // All three SDK methods shallow-merge options; per-call customHeaders
    // replaces the model's entire header collection, even when undefined.
    const options = {
      ...model._requestOptions,
      ...(requestOptions as object),
    };
    const headers = options.customHeaders;
    if (headers != null) {
      const entries =
        typeof Headers !== "undefined" && headers instanceof Headers
          ? headers.entries()
          : Array.isArray(headers)
            ? headers
            : Object.entries(headers);
      for (const [name, value] of entries) {
        if (typeof name !== "string" || !sensitiveHeader.test(name.trim()))
          continue;
        if (typeof value !== "string") return null;
        credentials.push(value);
        // Transport errors may quote an authorization token without its scheme.
        if (/^(?:proxy-)?authorization$/i.test(name.trim()))
          credentials.push(value.replace(/^\s*(?:Bearer|Basic)\s+/i, ""));
      }
    }
    return credentials;
  } catch {
    return null;
  }
}

// Older SDKs put the API key in request URLs. Newer ones can quote API keys and
// custom headers in validation errors. This is independent of content capture.
function redact(text: string, credentials: readonly string[]): string {
  const variants = new Set<string>();
  for (const credential of credentials) {
    for (const value of [credential, credential.trim()]) {
      variants.add(value);
      variants.add(JSON.stringify(value).slice(1, -1));
      variants.add(encodeURIComponent(value));
    }
  }
  // Sort across all credentials to avoid exposing suffixes of overlapping values.
  for (const secret of [...variants].sort((a, b) => b.length - a.length)) {
    if (secret) text = text.split(secret).join(REDACTED_VALUE);
  }
  return text
    .replace(
      /([?&](?:key|api[_-]?key|access_token|token|secret)=)[^&#\s"'<>]*/gi,
      (_, field: string) => field + REDACTED_VALUE,
    )
    .replace(
      /(\b(?:x-goog-api-key|x-api-key|api[_-]?key|authorization|proxy-authorization|x-secret-key|x-auth-token|x-access-token|cookie|set-cookie)["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\r\n,;}]+)/gi,
      (_, field: string) => field + REDACTED_VALUE,
    );
}

/** Make a telemetry-only copy; never mutate or export the application error. */
export function telemetryError(
  error: unknown,
  credentials: RequestCredentials,
): { name?: string; message: string; stack?: string } {
  const fallback = { message: "Google GenAI request failed" };
  if (credentials === null) return fallback;
  try {
    if (error instanceof Error) {
      return {
        name: redact(error.name, credentials),
        message: redact(error.message, credentials),
        stack:
          error.stack === undefined
            ? undefined
            : redact(error.stack, credentials),
      };
    }
    return { message: redact(String(error), credentials) };
  } catch {
    // A hostile getter/toString or invalid Unicode credential must fail closed,
    // without changing the original rejection or losing the ERROR status.
    return fallback;
  }
}
