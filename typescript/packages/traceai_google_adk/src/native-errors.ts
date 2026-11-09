import { SpanStatusCode, trace } from "@opentelemetry/api";

/**
 * ADK 2.4's BasePlugin.onModelErrorCallback hook. Assign this method on a
 * BasePlugin subclass registered first on the Runner. It enriches the active
 * native call_llm span without creating spans or changing ADK's error handling.
 *
 * The structural parameter type keeps the legacy ADK floor importable; this
 * hook itself requires ADK 2.4.0. Error text is never recorded, even in process.
 */
export async function recordGoogleADKModelError({ llmRequest, error }: {
  llmRequest: { model?: string };
  error: Error;
}): Promise<undefined> {
  const span = trace.getActiveSpan();
  if (!span?.isRecording()) return undefined;
  span.setStatus({ code: SpanStatusCode.ERROR });
  span.setAttribute("error.type", error.name || "Error");
  span.setAttribute("gen_ai.system", "gcp.vertex.agent");
  if (llmRequest.model) span.setAttribute("gen_ai.request.model", llmRequest.model);
  // GenAI ApiError exposes its HTTP status separately from its sensitive body.
  // Do not parse the message or infer an error from absent usage/model metadata.
  const status = (error as Error & { status?: unknown }).status;
  if (typeof status === "number" && Number.isInteger(status) && status >= 100 && status <= 599) {
    span.setAttribute("http.response.status_code", status);
  }
  return undefined;
}
