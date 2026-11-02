import { REDACTED_VALUE } from "@traceai/fi-core";

// Older SDKs put the API key in request URLs. Newer ones can quote it in
// header-validation errors. Credential redaction is independent of content capture.
function redact(text: string, apiKey: unknown): string {
  if (typeof apiKey === "string" && apiKey) {
    const variants = new Set([
      apiKey,
      apiKey.trim(),
      JSON.stringify(apiKey).slice(1, -1),
      encodeURIComponent(apiKey),
    ]);
    // Replace longer variants first (e.g. a JSON-escaped multiline credential).
    for (const secret of [...variants].sort((a, b) => b.length - a.length)) {
      if (secret) text = text.split(secret).join(REDACTED_VALUE);
    }
  }
  return text
    .replace(
      /([?&](?:key|api[_-]?key|access_token|token|secret)=)[^&#\s"'<>]*/gi,
      (_, field: string) => field + REDACTED_VALUE,
    )
    .replace(
      /(\b(?:x-goog-api-key|x-api-key|api[_-]?key|authorization|proxy-authorization)["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\r\n,;}]+)/gi,
      (_, field: string) => field + REDACTED_VALUE,
    );
}

/** Make a telemetry-only copy; never mutate or export the application error. */
export function telemetryError(
  error: unknown,
  apiKey: unknown,
): { name?: string; message: string; stack?: string } {
  try {
    if (error instanceof Error) {
      return {
        name: redact(error.name, apiKey),
        message: redact(error.message, apiKey),
        stack:
          error.stack === undefined ? undefined : redact(error.stack, apiKey),
      };
    }
    return { message: redact(String(error), apiKey) };
  } catch {
    // A hostile getter/toString or invalid Unicode credential must fail closed,
    // without changing the original rejection or losing the ERROR status.
    return { message: "Google GenAI request failed" };
  }
}
