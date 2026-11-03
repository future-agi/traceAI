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

/** Read only a bounded JSON clone; do not depend on Content-Length (chunked responses omit it). */
async function responseMetadata(response: Response): Promise<unknown> {
  const reader = response.clone().body?.getReader();
  if (!reader) return;
  const parts: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 65536) return;
      parts.push(value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const part of parts) { bytes.set(part, offset); offset += part.byteLength; }
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch { return; } // Metadata is best effort; the SDK still receives the original body.
  finally { void reader.cancel().catch(() => {}); }
}

/** Observe complete bounded SSE frames, without retaining message content. */
function observer(span: Span, markError: () => void) {
  const decoder = new TextDecoder();
  let pending = "";
  return (chunk: Uint8Array) => {
    pending += decoder.decode(chunk, { stream: true });
    if (pending.length > 65536) { pending = ""; return; }
    // Match the SDK's SSE boundary variants, including CRLF and LF-only frames.
    const boundary = /\r\n\r\n|\r\n\r|\r\n\n|\r\r\n|\n\r\n|\r\r|\n\r|\n\n/g;
    let start = 0;
    for (const match of pending.matchAll(boundary)) {
      const frame = pending.slice(start, match.index);
      start = match.index! + match[0].length;
      const data = frame.split(/\r\n|\r|\n/).filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
      if (!data || data === "[DONE]") continue;
      try {
        const event = object(JSON.parse(data));
        // Chat chunks carry usage directly; Responses events carry the response in a nested object.
        setOutput(span, event?.response ?? event);
      } catch { markError(); /* The SDK will report the parsing failure to its caller. */ }
    }
    pending = pending.slice(start);
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
        const observe = observer(span, () => span.setStatus({ code: SpanStatusCode.ERROR }));
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
      // A bounded JSON clone does not consume the SDK's response body.
      if (type.includes("application/json")) setOutput(span, await responseMetadata(response));
      end();
      return response;
    } catch (error) { end(true); throw error; }
  } });
}
