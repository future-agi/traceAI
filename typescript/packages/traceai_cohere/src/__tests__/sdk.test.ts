import { createServer, Server } from "node:http";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import { CohereInstrumentation, isPatched } from "../instrumentation";
import { context, trace, SpanStatusCode } from "@opentelemetry/api";
import { suppressTracing } from "@opentelemetry/core";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";

// Real SDK serializers and transports, with an offline localhost API double.
const sdk8 = require("cohere-ai");
const sdk7 = require("cohere-ai-v7");
const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
let server: Server;
let url: string;
let response: unknown;
let contentType = "application/json";
let status = 200;
let received: { path: string; body: any; headers: unknown }[] = [];
let wire: any[] = [];
let instrumentation: CohereInstrumentation;
const v1Response = { text: "hello", generation_id: "generation", meta: { tokens: { input_tokens: 5, output_tokens: 2 } } };
const v2Response = { id: "generation", finish_reason: "COMPLETE", message: { role: "assistant", content: [{ type: "text", text: "hello" }] }, usage: { tokens: { input_tokens: 5, output_tokens: 2 } } };

beforeAll(async () => {
  provider.register();
  server = createServer((request, reply) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      const body = JSON.parse(Buffer.concat(chunks).toString() || "{}");
      if (request.url === "/v1/traces") {
        wire.push(body);
        reply.setHeader("Content-Type", "application/json");
        reply.end("{}");
        return;
      }
      received.push({ path: request.url!, body, headers: request.headers });
      reply.statusCode = status;
      reply.setHeader("Content-Type", contentType);
      reply.end(typeof response === "string" ? response : JSON.stringify(response));
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  url = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
});
beforeEach(() => {
  exporter.reset();
  wire = [];
  received = [];
  status = 200;
  contentType = "application/json";
  instrumentation = new CohereInstrumentation();
  instrumentation.setTracerProvider(provider);
});
afterEach(() => instrumentation.disable());
afterAll(async () => {
  await new Promise<void>((resolve) => server.close(() => resolve()));
  await provider.shutdown();
});

it("traces CohereClientV2's generated v2 client", async () => {
  instrumentation.manuallyInstrument(sdk8);
  response = v2Response;
  const client = new sdk8.CohereClientV2({ token: "offline-placeholder", environment: url });
  const result = await client.chat({ model: "command-test", messages: [{ role: "user", content: "question" }] });
  expect(result.message.content[0].text).toBe("hello");
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].attributes[S.LLM_TOKEN_COUNT_PROMPT]).toBe(5);
});

it.each([["7.0.0", sdk7], ["8.3.0", sdk8]])("reads the real %s SDK camelCase chat history", async (_version, sdk) => {
  instrumentation.manuallyInstrument(sdk);
  response = v1Response;
  const client = new sdk.CohereClient({ token: "offline-placeholder", environment: url });
  await client.chat({ model: "command-test", message: "question", chatHistory: [{ role: "USER", message: "earlier" }] });
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].attributes[S.LLM_INPUT_MESSAGES]).toContain("earlier");
});

function clientFor(sdk: any, v2 = false): any {
  instrumentation.manuallyInstrument(sdk);
  return new (v2 ? sdk.CohereClientV2 : sdk.CohereClient)({ token: "offline-placeholder", environment: url });
}
const request = { model: "command-test", message: "question" };
const requestV2 = { model: "command-test", messages: [{ role: "user", content: "question" }] };
function streamResponse(v2: boolean) {
  const events = v2 ? [
    { type: "content-delta", index: 0, delta: { message: { content: { text: "hel" } } } },
    { type: "content-delta", index: 0, delta: { message: { content: { text: "lo" } } } },
    { type: "tool-call-start", index: 0, delta: { message: { tool_calls: { id: "call-1", type: "function", function: { name: "lookup", arguments: "{\"key\":" } } } } },
    { type: "tool-call-delta", index: 0, delta: { message: { tool_calls: { function: { arguments: "\"value\"}" } } } } },
    { type: "message-end", delta: { finish_reason: "COMPLETE", usage: { tokens: { input_tokens: 5, output_tokens: 2 } } } },
  ] : [
    { event_type: "text-generation", text: "hel" },
    { event_type: "text-generation", text: "lo" },
    { event_type: "stream-end", finish_reason: "COMPLETE", response: v1Response },
  ];
  contentType = v2 ? "text/event-stream" : "application/x-ndjson";
  response = events.map((event) => v2 ? `data: ${JSON.stringify(event)}\n\n` : `${JSON.stringify(event)}\n`).join("");
  return events;
}

it.each([false, true])("traces real v2=%s SDK streaming through terminal usage and preserves helpers", async (v2) => {
  const client = clientFor(sdk8, v2);
  const expected = streamResponse(v2);
  const pending = client.chatStream(v2 ? requestV2 : request);
  expect(typeof pending.withRawResponse).toBe("function");
  const { data: stream, rawResponse } = await pending.withRawResponse();
  expect(rawResponse.status).toBe(200);
  expect(await pending).toBe(stream);
  expect(stream.controller).toBeInstanceOf(AbortController);
  expect(exporter.getFinishedSpans()).toHaveLength(0);
  const events = [];
  for await (const event of stream) events.push(event);
  expect(events).toHaveLength(expected.length);
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(1);
  expect(spans[0].attributes[S.OUTPUT_VALUE]).toBe("hello");
  expect(spans[0].attributes[S.LLM_TOKEN_COUNT_PROMPT]).toBe(5);
  expect(spans[0].attributes[S.LLM_TOKEN_COUNT_COMPLETION]).toBe(2);
  expect(spans[0].status.code).toBe(SpanStatusCode.OK);
  if (v2) {
    const messages = JSON.parse(spans[0].attributes[S.LLM_OUTPUT_MESSAGES] as string);
    expect(messages[0].tool_calls[0]).toEqual({ id: "call-1", type: "function", function: { name: "lookup", arguments: '{"key":"value"}' } });
  }
});

it.each([false, true])("closes a real v2=%s SDK stream when the consumer breaks early", async (v2) => {
  const client = clientFor(sdk8, v2);
  streamResponse(v2);
  const stream = await client.chatStream(v2 ? requestV2 : request);
  for await (const _event of stream) break;
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].attributes["cohere.stream.cancelled"]).toBe(true);
  expect(exporter.getFinishedSpans()[0].attributes[S.OUTPUT_VALUE]).toBe("hel");
  expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.UNSET);
});

it("finalizes on abort before iteration, without consuming events", async () => {
  const client = clientFor(sdk8, true);
  streamResponse(true);
  const stream = await client.chatStream(requestV2);
  stream.controller.abort();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].attributes["cohere.stream.cancelled"]).toBe(true);
});

it("finalizes on return before first next", async () => {
  const client = clientFor(sdk8, true);
  streamResponse(true);
  const stream = await client.chatStream(requestV2);
  await stream[Symbol.asyncIterator]().return();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
});

it("records SDK stream parser errors and preserves the rejection", async () => {
  const client = clientFor(sdk8, true);
  contentType = "text/event-stream";
  response = "data: not-json\n\n";
  const stream = await client.chatStream(requestV2);
  await expect(stream[Symbol.asyncIterator]().next()).rejects.toBeInstanceOf(Error);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
});

it.each([["7.0.0", sdk7], ["8.3.0", sdk8]])("preserves real %s SDK embeddings and reranker types", async (_version, sdk) => {
  const client = clientFor(sdk);
  response = { id: "embedding", response_type: "embeddings_floats", texts: ["document"], embeddings: [[0.1, 0.2]], meta: { billed_units: { input_tokens: 3 } } };
  await client.embed({ texts: ["document"], model: "embed-test", inputType: "search_document" });
  response = { id: "rerank", results: [{ index: 0, relevance_score: 0.9, document: { text: "document" } }] };
  const result = await client.rerank({ model: "rerank-test", query: "query", documents: [{ text: "document" }], topN: 1 });
  expect(result.results[0].relevanceScore).toBe(0.9);
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(2);
  expect(spans[0].attributes[S.FI_SPAN_KIND]).toBe("EMBEDDING");
  expect(spans[0].attributes[`${S.EMBEDDING_EMBEDDINGS}.0.${S.EMBEDDING_VECTOR}`]).toEqual([0.1, 0.2]);
  expect(spans[1].attributes[S.FI_SPAN_KIND]).toBe("RERANKER");
  expect(spans[1].attributes["cohere.rerank.results.0.relevance_score"]).toBe(0.9);
  expect(spans[1].attributes["cohere.rerank.top_n"]).toBe(1);
  expect(received[1].body.top_n).toBe(1);
});

it("instruments v2 embeddings, rerank and client.v2 without duplicate spans", async () => {
  const client = clientFor(sdk8, true);
  response = { id: "embedding", texts: ["document"], embeddings: { float: [[0.1, 0.2]] }, meta: { billed_units: { input_tokens: 3 } } };
  await client.embed({ texts: ["document"], model: "embed-test", inputType: "search_document", embeddingTypes: ["float"] });
  response = { id: "rerank", results: [{ index: 0, relevance_score: 0.9 }] };
  await client.rerank({ model: "rerank-test", query: "query", documents: ["document"] });
  response = v2Response;
  const v1 = new sdk8.CohereClient({ token: "offline-placeholder", environment: url });
  const pending = v1.v2.chat(requestV2, { headers: { "X-Test-Forwarded": "yes" } });
  const raw = await pending.withRawResponse();
  expect(raw.data).toBe(await pending);
  expect(raw.rawResponse.status).toBe(200);
  expect(received[2].headers).toMatchObject({ "x-test-forwarded": "yes" });
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(3);
  expect(spans.map((span) => span.attributes[S.FI_SPAN_KIND])).toEqual(["EMBEDDING", "RERANKER", "LLM"]);
  expect(spans[0].attributes[S.LLM_TOKEN_COUNT_PROMPT]).toBe(3);
});

it("does not invent a model name when the caller leaves it to Cohere", async () => {
  const client = clientFor(sdk7);
  response = v1Response;
  await client.chat({ message: "question" });
  expect(exporter.getFinishedSpans()[0].attributes[S.LLM_MODEL_NAME]).toBeUndefined();
});

it("supports multiple SDK copies, idempotent manual patching and enable/disable", async () => {
  const client7 = clientFor(sdk7);
  const client8 = clientFor(sdk8, true);
  instrumentation.manuallyInstrument(sdk8);
  expect(isPatched()).toBe(true);
  response = v1Response;
  await client7.chat(request);
  response = v2Response;
  await client8.chat(requestV2);
  expect(exporter.getFinishedSpans()).toHaveLength(2);
  instrumentation.disable();
  expect(isPatched()).toBe(false);
  await client8.chat(requestV2); // already-bound method must respect disable
  instrumentation.enable();
  await client8.chat(requestV2);
  const newClient8 = clientFor(sdk8, true);
  await newClient8.chat(requestV2);
  expect(exporter.getFinishedSpans()).toHaveLength(4);
});

it("does not let a second instrumentor unwrap another owner's methods", async () => {
  const client = clientFor(sdk8, true);
  const other = new CohereInstrumentation();
  other.setTracerProvider(provider);
  other.manuallyInstrument(sdk8);
  other.disable();
  response = v2Response;
  await client.chat(requestV2);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
});

it("honors suppression and propagates the parent across concurrent SDK calls", async () => {
  const client = clientFor(sdk8, true);
  response = v2Response;
  await context.with(suppressTracing(context.active()), () => client.chat(requestV2));
  expect(exporter.getFinishedSpans()).toHaveLength(0);
  const parent = provider.getTracer("parent").startSpan("parent");
  await context.with(trace.setSpan(context.active(), parent), () => Promise.all([client.chat(requestV2), client.chat(requestV2)]));
  parent.end();
  const children = exporter.getFinishedSpans().filter((span) => span.name !== "parent");
  expect(children).toHaveLength(2);
  for (const child of children) expect(child.parentSpanContext?.spanId).toBe(parent.spanContext().spanId);
  expect(trace.getActiveSpan()).toBeUndefined();
});

it.each(["normal", "raw", "stream"])("records real SDK HTTP errors using the %s path once", async (mode) => {
  const client = clientFor(sdk8, true);
  status = 400;
  response = { message: "fixture-error" };
  const pending = mode === "stream" ? client.chatStream(requestV2) : client.chat(requestV2);
  await expect(mode === "raw" ? pending.withRawResponse() : pending).rejects.toBeInstanceOf(sdk8.CohereError);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
});

it.each(["content", "hidden", "vectors"])("exports correct shape and %s privacy over actual OTLP/HTTP", async (mode) => {
  instrumentation.disable();
  const wireProvider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(new OTLPTraceExporter({ url: `${url}/v1/traces` }))] });
  instrumentation = new CohereInstrumentation({ traceConfig: mode === "hidden" ? { hideInputs: true, hideOutputs: true } : { hideEmbeddingVectors: mode === "vectors" } });
  instrumentation.setTracerProvider(wireProvider);
  try {
    const client = clientFor(sdk8, true);
    response = { id: "embedding", texts: ["secret-input"], embeddings: { float: [[0.123456789]] } };
    await client.embed({ texts: ["secret-input"], model: "embed-test", inputType: "search_document", embeddingTypes: ["float"] });
    response = { id: "rerank", results: [{ index: 0, relevance_score: 0.9, document: { text: "secret-output" } }] };
    await client.rerank({ model: "rerank-test", query: "secret-input", documents: ["secret-input"], topN: 1 });
    streamResponse(true);
    response = String(response).replaceAll("hel", "secret-output");
    const stream = await client.chatStream({ ...requestV2, messages: [{ role: "user", content: "secret-input" }], tools: [{ type: "function", function: { name: "tool-secret-input", description: "secret-input", parameters: {} } }] });
    for await (const _event of stream) { /* consume */ }
    status = 400;
    response = { message: "secret-input secret-output" };
    await expect(client.chat(requestV2)).rejects.toBeInstanceOf(Error);
    await wireProvider.forceFlush();
    const encoded = JSON.stringify(wire);
    const spans = wire.flatMap((batch) => batch.resourceSpans.flatMap((resource: any) => resource.scopeSpans.flatMap((scope: any) => scope.spans)));
    expect(spans).toHaveLength(4);
    const attrs = (span: any) => Object.fromEntries(span.attributes.map((item: any) => [item.key, Object.values(item.value)[0]]));
    expect(attrs(spans[0])[S.FI_SPAN_KIND]).toBe("EMBEDDING");
    expect(attrs(spans[1])[S.FI_SPAN_KIND]).toBe("RERANKER");
    expect(attrs(spans[2])[S.LLM_TOKEN_COUNT_PROMPT]).toBe(5);
    expect(spans[3].status.code).toBe(SpanStatusCode.ERROR);
    if (mode === "hidden") {
      expect(encoded).not.toContain("secret-input");
      expect(encoded).not.toContain("secret-output");
      expect(encoded).not.toContain("0.123456789");
    } else {
      expect(encoded).toContain("secret-input");
      expect(encoded).toContain("secret-output");
    }
    if (mode === "vectors") expect(encoded).not.toContain("0.123456789");
  } finally {
    await wireProvider.shutdown();
  }
});
