import { context, trace, SpanKind, SpanStatusCode, type Span, type Tracer } from "@opentelemetry/api";
import { HTTPClient, type Fetcher } from "@openrouter/sdk";
// The SDK exports its generated response schemas through this public subpath. The
// workspace's CommonJS tsconfig uses legacy Node resolution, so load it at runtime.
// eslint-disable-next-line @typescript-eslint/no-require-imports
const sdkOperations = require("@openrouter/sdk/models/operations") as Record<string, { safeParse(value: unknown): { success: boolean } }>;

export const VERSION = "0.1.1"; // x-release-please-version

/** Only official SDK inference endpoints are traced; unrelated OpenRouter APIs are left alone. */
const operations: Readonly<Record<string, string>> = {
  "/chat/completions": "chat", "/embeddings": "embeddings", "/rerank": "rerank",
  "/responses": "responses", "/beta/responses": "responses",
};
const responseSchemas = {
  chat: sdkOperations.SendChatCompletionRequestResponse$inboundSchema,
  embeddings: sdkOperations.CreateEmbeddingsResponse$inboundSchema,
  rerank: sdkOperations.CreateRerankResponse$inboundSchema,
  responses: sdkOperations.CreateResponsesResponse$inboundSchema,
};
function spanKind(operation: string): string {
  return operation === "embeddings" ? "EMBEDDING" : operation === "rerank" ? "RERANKER" : "LLM";
}

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
async function responseMetadata(response: Response): Promise<{ payload?: unknown; malformed?: boolean }> {
  const reader = response.clone().body?.getReader();
  if (!reader) return {};
  const parts: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 65536) return {};
      parts.push(value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const part of parts) { bytes.set(part, offset); offset += part.byteLength; }
    try { return { payload: JSON.parse(new TextDecoder().decode(bytes)) }; }
    catch { return { malformed: true }; } // The SDK also rejects malformed JSON.
  } catch { return {}; } // A clone read failure does not prove that the SDK's own read failed.
  finally { void reader.cancel().catch(() => {}); }
}

/** Observe complete bounded SSE frames, without retaining message content. */
function observer(span: Span, markError: () => void, markDone: () => void = () => {}, markUnobserved: () => void = () => {}) {
  const decoder = new TextDecoder();
  let pending = "";
  return (chunk: Uint8Array) => {
    pending += decoder.decode(chunk, { stream: true });
    // An oversized frame is not observed, so the stream can no longer be shown to have succeeded.
    if (pending.length > 65536) { pending = ""; markUnobserved(); return; }
    // Match the SDK's SSE boundary variants, including CRLF and LF-only frames.
    const boundary = /\r\n\r\n|\r\n\r|\r\n\n|\r\r\n|\n\r\n|\r\r|\n\r|\n\n/g;
    let start = 0;
    for (const match of pending.matchAll(boundary)) {
      const frame = pending.slice(start, match.index);
      start = match.index! + match[0].length;
      const data = frame.split(/\r\n|\r|\n/).filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
      if (!data) continue;
      if (data === "[DONE]") { markDone(); continue; }
      try {
        const event = object(JSON.parse(data));
        if (event?.type === "error" || event?.type === "response.failed" || event?.type === "response.incomplete") markError();
        else if (event?.type === "response.completed") markDone();
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
      "fi.span.kind": spanKind(operation), "gen_ai.operation.name": name, "gen_ai.provider.name": "openrouter",
    } });
    const model = await requestModel(request);
    if (model) span.setAttribute("gen_ai.request.model", model);
    let ended = false;
    let failed = false;
    // OK needs positive evidence of a completed success, as in the other traceAI instrumentors: a
    // validated JSON body, or a stream's final [DONE] or response.completed event. A provider failure
    // seen in a stream stays ERROR; anything else keeps the default status.
    const end = (error = false, succeeded = false) => {
      if (ended) return;
      ended = true;
      if (error) span.setStatus({ code: SpanStatusCode.ERROR });
      else if (succeeded && !failed) span.setStatus({ code: SpanStatusCode.OK });
      span.end();
    };
    try {
      // Keep only the SDK's transport in scope; do not wrap a separate HTTP span.
      const response = await context.with(trace.setSpan(context.active(), span), () => fetcher(input, init));
      if (!response.ok) { span.setAttribute("http.response.status_code", response.status); end(true); return response; }
      const type = response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() ?? "";
      // These inference routes accept only 200 JSON or SSE in the official SDK.
      // Other successful HTTP statuses/content types are rejected by its matcher.
      if (response.status !== 200 || (type !== "application/json" && type !== "text/event-stream")) {
        end(true);
        return response;
      }
      if (type === "text/event-stream" && response.body) {
        const reader = response.body.getReader();
        let streamDone = false;
        let unobserved = false;
        let cancelling = false;
        const observe = observer(span, () => { failed = true; span.setStatus({ code: SpanStatusCode.ERROR }); }, () => { streamDone = true; }, () => { unobserved = true; });
        const body = new ReadableStream<Uint8Array>({
          async pull(controller) {
            try {
              const { done, value } = await reader.read();
              // A cancel resolves a pending read as done; cancel() ends the span with its reason.
              if (cancelling) return;
              if (done) { end(false, streamDone && !unobserved); controller.close(); return; }
              observe(value);
              controller.enqueue(value);
            } catch (error) { end(true); controller.error(error); }
          },
          // The SDK cancels the body after the final [DONE] or response.completed event; that is a completed stream.
          async cancel(reason) { cancelling = true; try { await reader.cancel(reason); } finally { end(reason instanceof Error, streamDone && !unobserved); } },
        }, { highWaterMark: 0 });
        return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
      }
      // A bounded JSON clone does not consume the SDK's response body.
      if (type === "application/json") {
        const metadata = await responseMetadata(response);
        if (metadata.malformed || (metadata.payload !== undefined && !responseSchemas[operation as keyof typeof responseSchemas].safeParse(metadata.payload).success)) {
          end(true);
        } else {
          setOutput(span, metadata.payload);
          // An oversized body skips the bounded validation, so it is not evidence of success.
          end(false, metadata.payload !== undefined);
        }
      } else end();
      return response;
    } catch (error) { end(true); throw error; }
  } });
}
