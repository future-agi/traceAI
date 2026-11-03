import { context, trace, SpanKind, SpanStatusCode, type Span, type Tracer } from "@opentelemetry/api";
import { HTTPClient, type Fetcher } from "@openrouter/sdk";

export const VERSION = "0.1.0"; // x-release-please-version

/** Only official SDK inference endpoints are traced; unrelated OpenRouter APIs are left alone. */
const operations: Readonly<Record<string, string>> = {
  "/chat/completions": "chat", "/embeddings": "embeddings", "/rerank": "rerank",
  "/responses": "responses", "/beta/responses": "responses",
};

type Usage = Record<string, unknown>;
const object = (value: unknown): Usage | undefined => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Usage : undefined;
function setOutput(span: Span, payload: unknown): void {
  const data = object(payload);
  if (!data) return;
  if (typeof data.model === "string") span.setAttribute("gen_ai.response.model", data.model);
  const usage = object(data.usage);
  if (!usage) return;
  for (const [field, attribute] of [
    ["prompt_tokens", "gen_ai.usage.input_tokens"], ["completion_tokens", "gen_ai.usage.output_tokens"],
    ["input_tokens", "gen_ai.usage.input_tokens"], ["output_tokens", "gen_ai.usage.output_tokens"],
  ]) {
    const count = usage[field];
    if (typeof count === "number" && Number.isSafeInteger(count) && count >= 0) span.setAttribute(attribute, count);
  }
}

/** Peek at a bounded clone so only the model identifier, never message content, becomes an attribute. */
async function requestModel(request: Request): Promise<string | undefined> {
  try {
    const reader = request.clone().body?.getReader();
    if (!reader) return;
    const parts: Uint8Array[] = [];
    let size = 0;
    while (size <= 65536) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 65536) { void reader.cancel().catch(() => {}); return; }
      parts.push(value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const part of parts) { bytes.set(part, offset); offset += part.byteLength; }
    const data = object(JSON.parse(new TextDecoder().decode(bytes)));
    // The generated SDK body uses model at the top level; do not log other fields.
    const model = data?.model;
    return typeof model === "string" && model.length <= 256 ? model : undefined;
  } catch { return; }
}

/** Parse bounded SSE event frames without retaining the stream or its content. */
function observer(span: Span) {
  const decoder = new TextDecoder();
  let pending = "";
  const line = (raw: string) => {
    const value = raw.trim();
    if (!value.startsWith("data:")) return;
    const data = value.slice(5).trim();
    if (data === "[DONE]" || data.length > 65536) return;
    try {
      const event = object(JSON.parse(data));
      // Chat chunks carry usage directly; Responses events carry the response in a nested object.
      setOutput(span, event?.response ?? event);
    } catch { /* malformed application data must not break inference */ }
  };
  return (chunk: Uint8Array) => {
    pending += decoder.decode(chunk, { stream: true });
    if (pending.length > 65536) { pending = ""; return; }
    const lines = pending.split(/\r?\n/);
    pending = lines.pop() ?? "";
    for (const item of lines) line(item);
  };
}

export interface OpenRouterTracingOptions {
  /** Supply a custom fetcher for SDK transports, including local test servers. */
  fetcher?: Fetcher;
  tracer?: Tracer;
}

/**
 * Pass this HTTP client to `new OpenRouter({ httpClient: createOpenRouterTracingClient() })`.
 * Do not also instrument this client's fetch transport with an LLM instrumentation.
 * Request/response content and provider errors are never recorded.
 */
export function createOpenRouterTracingClient(options: OpenRouterTracingOptions = {}): HTTPClient {
  const tracer = options.tracer ?? trace.getTracer("@traceai/openrouter", VERSION);
  const fetcher = options.fetcher ?? ((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init));
  return new HTTPClient({ fetcher: async (input, init) => {
    const request = input instanceof Request ? input : new Request(input, init);
    let operation: string | undefined;
    try {
      const url = new URL(request.url);
      operation = request.method === "POST" ? operations[url.pathname.replace(/^\/api\/v1/, "")] : undefined;
    } catch { /* preserve transport behavior on unexpected Request implementations */ }
    if (!operation) return fetcher(input, init);
    // callModel sends through the Responses route; mark the same span rather than adding a duplicate.
    const name = request.headers.get("x-openrouter-callmodel") === "true" ? "callModel" : operation;
    const span = tracer.startSpan(`${name} openrouter`, { kind: SpanKind.CLIENT, attributes: {
      "fi.span.kind": "LLM", "gen_ai.operation.name": name, "gen_ai.provider.name": "openrouter",
    } });
    const model = await requestModel(request);
    if (model) span.setAttribute("gen_ai.request.model", model);
    let ended = false;
    const end = (error = false) => { if (!ended) { ended = true; if (error) span.setStatus({ code: SpanStatusCode.ERROR }); span.end(); } };
    try {
      // Keep only the SDK's transport in scope; do not wrap a separate HTTP span.
      const response = await context.with(trace.setSpan(context.active(), span), () => fetcher(input, init));
      if (!response.ok) { span.setAttribute("http.response.status_code", response.status); end(true); return response; }
      const type = response.headers.get("content-type")?.toLowerCase() ?? "";
      if (type.includes("text/event-stream") && response.body) {
        const reader = response.body.getReader();
        const observe = observer(span);
        const body = new ReadableStream<Uint8Array>({
          async pull(controller) {
            try {
              const { done, value } = await reader.read();
              if (done) { end(); controller.close(); return; }
              observe(value);
              controller.enqueue(value);
            } catch (error) { end(true); controller.error(error); }
          },
          async cancel(reason) { try { await reader.cancel(reason); } finally { end(); } },
        }, { highWaterMark: 0 });
        return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
      }
      // A bounded JSON clone does not delay or consume the SDK's response body.
      const length = Number(response.headers.get("content-length"));
      if (type.includes("application/json") && length > 0 && length <= 65536) {
        try { setOutput(span, await response.clone().json()); } catch { /* metadata is best effort */ }
      }
      end();
      return response;
    } catch (error) { end(true); throw error; }
  } });
}
