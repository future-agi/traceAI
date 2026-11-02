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


it.each([[false, false], [false, true], [true, false], [true, true]])(
  "marks premature EOF as incomplete (v2=%s, empty=%s) without changing SDK iteration",
  async (v2, empty) => {
    const client = clientFor(sdk8, v2);
    const events = streamResponse(v2);
    response = empty ? "" : (v2 ? `data: ${JSON.stringify(events[0])}\n\n` : `${JSON.stringify(events[0])}\n`);
    const delivered = [];
    for await (const event of await client.chatStream(v2 ? requestV2 : request)) delivered.push(event);
    expect(delivered).toHaveLength(empty ? 0 : 1);
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.ERROR);
    expect(spans[0].status.message).toBe("Cohere stream ended without a terminal event");
    expect(spans[0].attributes[S.OUTPUT_VALUE]).toBe(empty ? "" : "hel");
    expect(spans[0].attributes[S.LLM_TOKEN_COUNT_PROMPT]).toBeUndefined();
  },
);

const privacyClients = [
  ["7.0.0 v1", sdk7, false], ["8.3.0 v1", sdk8, false], ["8.3.0 v2", sdk8, true],
] as const;
describe.each(privacyClients)("%s response privacy", (_label, sdk, v2) => {
  it.each(["hideInputs", "hideInputMessages", "hideInputText", "hideInputImages"])(
    "removes response-side input echoes with %s but retains generated output on the OTLP wire",
    async (flag) => {
      instrumentation.disable();
      const exporter = new InMemorySpanExporter();
      const wireProvider = new NodeTracerProvider({ spanProcessors: [
        new SimpleSpanProcessor(exporter),
        new SimpleSpanProcessor(new OTLPTraceExporter({ url: `${url}/v1/traces` })),
      ] });
      instrumentation = new CohereInstrumentation({ traceConfig: { [flag]: true } });
      instrumentation.setTracerProvider(wireProvider);
      try {
        const client = clientFor(sdk, v2);
        response = v2 ? { ...v2Response, message: { role: "assistant", content: [{ type: "text", text: "generated-answer" }],
          citations: [{ start: 0, end: 6, text: "generated-answer", sources: [{ type: "document", id: "doc", document: { text: "private-input-marker" } }] }] } }
          : { ...v1Response, text: "generated-answer", chat_history: [
            { role: "USER", message: "private-input-marker" },
            { role: "CHATBOT", message: "private-input-marker-previous-answer" },
            { role: "CHATBOT", message: "generated-answer" },
          ] };
        const result = await client.chat(v2 ? { ...requestV2, messages: [{ role: "user", content: "private-input-marker" }] } : { ...request, message: "private-input-marker" });
        expect(v2 ? result.message.content[0].text : result.text).toBe("generated-answer");
        // Sanitizing telemetry must not alter the SDK's response object.
        expect(JSON.stringify(result)).toContain("private-input-marker");
        response = { id: "embedding", response_type: "embeddings_floats", texts: ["private-input-marker"], embeddings: v2 ? { float: [[0.123456789]] } : [[0.123456789]] };
        await client.embed({ texts: ["private-input-marker"], model: "embed-test", inputType: "search_document", ...(v2 ? { embeddingTypes: ["float"] } : {}) });
        response = { id: "rerank", results: [{ index: 0, relevance_score: 0.9, document: { text: "private-input-marker" } }] };
        await client.rerank({ model: "rerank-test", query: "private-input-marker", documents: v2 ? ["private-input-marker"] : [{ text: "private-input-marker" }] });
        await wireProvider.forceFlush();
        const spans = exporter.getFinishedSpans();
        expect(spans).toHaveLength(3);
        expect(JSON.stringify(spans.map(span => span.attributes))).not.toContain("private-input-marker");
        expect(JSON.stringify(wire)).not.toContain("private-input-marker");
        expect(JSON.stringify(wire)).toContain("generated-answer");
        expect(spans[0].attributes[S.OUTPUT_VALUE]).toBe("generated-answer");
        expect(spans[0].attributes[S.LLM_TOKEN_COUNT_PROMPT]).toBe(5);
        expect(spans[1].attributes[`${S.EMBEDDING_EMBEDDINGS}.0.${S.EMBEDDING_VECTOR}`]).toEqual([0.123456789]);
        expect(spans[2].attributes["cohere.rerank.results.0.relevance_score"]).toBe(0.9);
      } finally {
        await wireProvider.shutdown();
      }
    },
  );

  it.each(["hideOutputs", "hideOutputMessages", "hideOutputText"])("keeps inputs visible when only %s is set", async (flag) => {
    instrumentation.disable();
    instrumentation = new CohereInstrumentation({ traceConfig: { [flag]: true } });
    instrumentation.setTracerProvider(provider);
    const client = clientFor(sdk, v2);
    response = v2 ? v2Response : v1Response;
    await client.chat(v2 ? requestV2 : request);
    const attrs = exporter.getFinishedSpans()[0].attributes;
    expect(attrs[S.INPUT_VALUE]).toContain("question");
    expect(attrs[S.RAW_OUTPUT]).toBeUndefined();
    expect(attrs[S.OUTPUT_VALUE]).toBeUndefined();
    expect(attrs[S.LLM_TOKEN_COUNT_PROMPT]).toBe(5);
  });
});

it("hides v1 terminal stream response echoes without losing generated text", async () => {
  instrumentation.disable();
  instrumentation = new CohereInstrumentation({ traceConfig: { hideInputs: true } });
  instrumentation.setTracerProvider(provider);
  const client = clientFor(sdk8);
  contentType = "application/x-ndjson";
  response = JSON.stringify({ event_type: "stream-end", finish_reason: "COMPLETE", response: {
    ...v1Response, chat_history: [{ role: "USER", message: "private-input-marker" }],
  } }) + "\n";
  for await (const _event of await client.chatStream({ ...request, message: "private-input-marker" })) { /* consume */ }
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(1);
  expect(JSON.stringify(spans[0].attributes)).not.toContain("private-input-marker");
  expect(spans[0].attributes[S.OUTPUT_VALUE]).toBe("hello");
  expect(spans[0].status.code).toBe(SpanStatusCode.OK);
});

// Connector authentication is not user content and must never enter telemetry.
describe.each(["capture", "hideOutputs", "hideInputs"])("8.3.0 connector credentials (%s)", (mode) => {
  it.each([false, true])("keeps credentials on the vendor request only (stream=%s)", async (streaming) => {
    instrumentation.disable();
    const captured = new InMemorySpanExporter();
    const wireProvider = new NodeTracerProvider({ spanProcessors: [
      new SimpleSpanProcessor(captured),
      new SimpleSpanProcessor(new OTLPTraceExporter({ url: `${url}/v1/traces` })),
    ] });
    instrumentation = new CohereInstrumentation({ traceConfig: mode === "capture" ? {} : { [mode]: true } });
    instrumentation.setTracerProvider(wireProvider);
    try {
      const client = clientFor(sdk8);
      const connector = Object.freeze({ id: "fixture-connector", userAccessToken: "synthetic-connector-credential", continueOnFailure: true, options: { site: "example.test" } });
      const input = Object.freeze({ ...request, connectors: Object.freeze([connector]) });
      if (streaming) {
        streamResponse(false);
        for await (const _event of await client.chatStream(input)) { /* consume */ }
      } else {
        response = v1Response;
        await client.chat(input);
      }
      await wireProvider.forceFlush();
      expect(received).toHaveLength(1);
      expect(received[0].body.connectors[0].user_access_token).toBe(connector.userAccessToken);
      expect(input.connectors[0]).toBe(connector);
      expect(input.connectors[0].userAccessToken).toBe("synthetic-connector-credential");
      const spans = captured.getFinishedSpans();
      expect(spans).toHaveLength(1);
      expect(JSON.stringify(spans[0].attributes)).not.toContain(connector.userAccessToken);
      expect(wire).not.toHaveLength(0);
      expect(JSON.stringify(wire)).not.toContain(connector.userAccessToken);
      if (mode === "hideInputs") {
        expect(spans[0].attributes[S.RAW_INPUT]).toBeUndefined();
      } else {
        expect(JSON.parse(spans[0].attributes[S.RAW_INPUT] as string).connectors).toEqual([
          { id: connector.id, continueOnFailure: true, options: { site: "example.test" } },
        ]);
      }
    } finally {
      await wireProvider.shutdown();
    }
  });
});

const finishCases = [
  ...[false, true].flatMap(v2 => [false, true].flatMap(streaming =>
    (v2 ? ["COMPLETE", "STOP_SEQUENCE", "MAX_TOKENS", "TOOL_CALL", "ERROR", "TIMEOUT"]
      : ["COMPLETE", "STOP_SEQUENCE", "MAX_TOKENS", "ERROR", "ERROR_TOXIC", "ERROR_LIMIT", "TIMEOUT", "USER_CANCEL"])
      .map(reason => ({ label: `8.3.0 v${v2 ? 2 : 1}`, sdk: sdk8, v2, streaming, reason })))),
];
describe.each([false, true])("finish reasons with hideOutputs=%s", (hideOutputs) => {
  it.each(finishCases)("classifies $label stream=$streaming $reason without changing SDK results", async ({ sdk, v2, streaming, reason }) => {
    instrumentation.disable();
    instrumentation = new CohereInstrumentation({ traceConfig: { hideOutputs } });
    instrumentation.setTracerProvider(provider);
    const client = clientFor(sdk, v2);
    const failed = ["ERROR", "ERROR_TOXIC", "ERROR_LIMIT", "TIMEOUT"].includes(reason);
    const cancelled = reason === "USER_CANCEL";
    if (streaming) {
      const events = streamResponse(v2);
      if (v2) (events[events.length - 1] as any).delta.finish_reason = reason;
      else (events[events.length - 1] as any).finish_reason = reason;
      response = events.map(event => v2 ? `data: ${JSON.stringify(event)}\n\n` : `${JSON.stringify(event)}\n`).join("");
      const delivered = [];
      for await (const event of await client.chatStream(v2 ? requestV2 : request)) delivered.push(event);
      expect(delivered).toHaveLength(events.length);
      const last = delivered[delivered.length - 1];
      expect(v2 ? last.delta.finishReason : last.finishReason).toBe(reason);
    } else {
      response = { ...(v2 ? v2Response : v1Response), finish_reason: reason };
      const pending = client.chat(v2 ? requestV2 : request);
      const result = await pending;
      expect(result.finishReason).toBe(reason);
      expect(v2 ? result.message.content[0].text : result.text).toBe("hello");
      expect((await pending.withRawResponse()).data).toBe(result);
    }
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(failed ? SpanStatusCode.ERROR : cancelled ? SpanStatusCode.UNSET : SpanStatusCode.OK);
    expect(spans[0].attributes[streaming ? "cohere.stream.cancelled" : "cohere.request.cancelled"]).toBe(cancelled ? true : undefined);
    expect(spans[0].events.filter(event => event.name === "exception")).toHaveLength(failed ? 1 : 0);
    expect(spans[0].attributes[S.OUTPUT_VALUE]).toBe(hideOutputs ? undefined : "hello");
    expect(spans[0].attributes[S.LLM_TOKEN_COUNT_PROMPT]).toBe(5);
  });
});

it.each(["complete", "start-only", "cancelled", "eof"])("captures v2 content-start text for %s streams", async (mode) => {
  const client = clientFor(sdk8, true);
  const events: any[] = [{ type: "content-start", index: 0, delta: { message: { content: { type: "text", text: "initial-" } } } }];
  if (mode === "complete") events.push({ type: "content-delta", index: 0, delta: { message: { content: { text: "final" } } } });
  if (mode !== "eof") events.push({ type: "message-end", delta: { finish_reason: "COMPLETE" } });
  contentType = "text/event-stream";
  response = events.map(event => `data: ${JSON.stringify(event)}\n\n`).join("");
  const delivered = [];
  for await (const event of await client.chatStream(requestV2)) {
    delivered.push(event);
    if (mode === "cancelled") break;
  }
  expect(delivered[0].delta.message.content.text).toBe("initial-");
  expect(delivered).toHaveLength(mode === "cancelled" ? 1 : events.length);
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(1);
  const text = mode === "complete" ? "initial-final" : "initial-";
  expect(spans[0].attributes[S.OUTPUT_VALUE]).toBe(text);
  expect(JSON.parse(spans[0].attributes[S.LLM_OUTPUT_MESSAGES] as string)[0].content).toEqual([{ type: "text", text }]);
  expect(JSON.parse(spans[0].attributes[S.RAW_OUTPUT] as string).text).toBe(text);
  expect(spans[0].status.code).toBe(mode === "eof" ? SpanStatusCode.ERROR : mode === "cancelled" ? SpanStatusCode.UNSET : SpanStatusCode.OK);
});


// Generated reasoning and partial tool arguments are output, not input echoes.
const outputModes = ["capture", "hideInputs", "hideOutputs", "hideOutputMessages", "hideOutputText"];
describe.each(outputModes)("generated content with %s", (mode) => {
  const hidden = mode.startsWith("hideOutput");
  async function captureOutput(action: (spans: InMemorySpanExporter) => Promise<void>) {
    instrumentation.disable();
    const captured = new InMemorySpanExporter();
    const wireProvider = new NodeTracerProvider({ spanProcessors: [
      new SimpleSpanProcessor(captured),
      new SimpleSpanProcessor(new OTLPTraceExporter({ url: `${url}/v1/traces` })),
    ] });
    instrumentation = new CohereInstrumentation({ traceConfig: mode === "capture" ? {} : { [mode]: true } });
    instrumentation.setTracerProvider(wireProvider);
    try {
      await action(captured);
      await wireProvider.forceFlush();
      expect(wire).not.toHaveLength(0);
      expect(JSON.stringify(wire).includes("synthetic-generated-")).toBe(!hidden);
    } finally {
      await wireProvider.shutdown();
    }
  }

  it.each(["chat", "complete", "cancelled", "eof", "start-only"])("preserves v2 reasoning blocks (%s)", async (ending) => {
    await captureOutput(async (captured) => {
      const client = clientFor(sdk8, true);
      const thinking = "synthetic-generated-reasoning-";
      const blocks = [
        { type: "thinking", thinking: thinking + (ending === "start-only" ? "" : "continued") },
        { type: "text", text: "visible-answer" },
        { type: "thinking", thinking: "second-thought" },
      ];
      if (ending === "chat") {
        response = { ...v2Response, message: { role: "assistant", content: blocks } };
        const result = await client.chat({ ...requestV2, thinking: { type: "enabled" } });
        expect(result.message.content).toEqual(blocks);
      } else {
        const events: any[] = [
          { type: "content-start", index: 0, delta: { message: { content: { type: "thinking", thinking } } } },
        ];
        if (ending !== "start-only") events.push(
          { type: "content-delta", index: 0, delta: { message: { content: { thinking: "continued" } } } },
          { type: "content-end", index: 0 },
          { type: "content-start", index: 1, delta: { message: { content: blocks[1] } } },
          // A delta without a start must also retain its generated content.
          { type: "content-delta", index: 2, delta: { message: { content: { thinking: "second-thought" } } } },
        );
        if (ending === "complete") events.push({ type: "message-end", delta: { finish_reason: "COMPLETE" } });
        contentType = "text/event-stream";
        response = events.map(event => `data: ${JSON.stringify(event)}\n\n`).join("");
        const delivered = [];
        for await (const event of await client.chatStream({ ...requestV2, thinking: { type: "enabled" } })) {
          delivered.push(event);
          if (ending === "cancelled" && delivered.length === 2) break;
        }
        expect(delivered[0].delta.message.content.thinking).toBe(thinking);
        expect(delivered).toHaveLength(ending === "cancelled" ? 2 : events.length);
      }
      const spans = captured.getFinishedSpans();
      expect(spans).toHaveLength(1);
      const attrs = spans[0].attributes;
      expect(spans[0].status.code).toBe(ending === "cancelled" ? SpanStatusCode.UNSET : ["eof", "start-only"].includes(ending) ? SpanStatusCode.ERROR : SpanStatusCode.OK);
      if (hidden) {
        expect(attrs[S.LLM_OUTPUT_MESSAGES]).toBeUndefined();
        expect(attrs[S.RAW_OUTPUT]).toBeUndefined();
        expect(JSON.stringify(attrs)).not.toContain(thinking);
      } else {
        const expectedBlocks = ["cancelled", "start-only"].includes(ending) ? blocks.slice(0, 1) : blocks;
        expect(JSON.parse(attrs[S.LLM_OUTPUT_MESSAGES] as string)[0].content).toEqual(expectedBlocks);
        expect(JSON.parse(attrs[S.RAW_OUTPUT] as string).message.content).toEqual(expectedBlocks);
        expect(attrs[S.OUTPUT_VALUE]).toBe(expectedBlocks.length === 1 ? "" : "visible-answer");
      }
    });
  });

  it.each(["chat", "complete", "cancelled", "eof"])("retains v2 generated tool plans (%s)", async (ending) => {
    await captureOutput(async (captured) => {
      const client = clientFor(sdk8, true);
      const plan = "synthetic-generated-tool-plan-continued";
      if (ending === "chat") {
        response = { ...v2Response, message: { role: "assistant", tool_plan: plan, tool_calls: [{ id: "call-1", type: "function", function: { name: "lookup", arguments: "{}" } }] } };
        const result = await client.chat(requestV2);
        expect(result.message.toolPlan).toBe(plan);
      } else {
        const events: any[] = [
          { type: "tool-plan-delta", delta: { message: { tool_plan: "synthetic-generated-tool-plan-" } } },
          { type: "tool-plan-delta", delta: { message: { tool_plan: "continued" } } },
          { type: "tool-call-start", index: 0, delta: { message: { tool_calls: { id: "call-1", type: "function", function: { name: "lookup", arguments: "{}" } } } } },
        ];
        if (ending === "complete") events.push({ type: "message-end", delta: { finish_reason: "TOOL_CALL" } });
        contentType = "text/event-stream";
        response = events.map(event => `data: ${JSON.stringify(event)}\n\n`).join("");
        const delivered = [];
        for await (const event of await client.chatStream(requestV2)) {
          delivered.push(event);
          if (ending === "cancelled" && delivered.length === 3) break;
        }
        expect(delivered[0].delta.message.toolPlan).toBe("synthetic-generated-tool-plan-");
      }
      const spans = captured.getFinishedSpans();
      expect(spans).toHaveLength(1);
      const attrs = spans[0].attributes;
      expect(spans[0].status.code).toBe(ending === "cancelled" ? SpanStatusCode.UNSET : ending === "eof" ? SpanStatusCode.ERROR : SpanStatusCode.OK);
      if (hidden) {
        expect(attrs[S.LLM_OUTPUT_MESSAGES]).toBeUndefined();
        expect(attrs[S.RAW_OUTPUT]).toBeUndefined();
        expect(JSON.stringify(attrs)).not.toContain("synthetic-generated-");
      } else {
        expect(JSON.parse(attrs[S.RAW_OUTPUT] as string).message.toolPlan).toBe(plan);
        expect(JSON.parse(attrs[S.LLM_OUTPUT_MESSAGES] as string)[0].tool_plan).toBe(plan);
        expect(attrs[S.OUTPUT_VALUE]).not.toContain(plan);
      }
    });
  });

  it.each(["cancelled", "eof", "terminal", "generation"])("retains v1 generated tool plans (%s)", async (ending) => {
    await captureOutput(async (captured) => {
      const client = clientFor(sdk8);
      const partial = "synthetic-generated-tool-plan-continued";
      const events: any[] = [
        { event_type: "tool-calls-chunk", tool_call_delta: { index: 0, text: "synthetic-generated-tool-plan-", name: "lookup", parameters: "{}" } },
        { event_type: "tool-calls-chunk", tool_call_delta: { index: 0, text: "continued" } },
      ];
      if (ending === "generation") events.push({ event_type: "tool-calls-generation", text: partial, tool_calls: [{ name: "lookup", parameters: {} }] });
      if (ending === "terminal") events.push({ event_type: "stream-end", finish_reason: "COMPLETE", response: { text: partial, tool_calls: [{ name: "lookup", parameters: {} }] } });
      contentType = "application/x-ndjson";
      response = events.map(event => `${JSON.stringify(event)}\n`).join("");
      const delivered = [];
      for await (const event of await client.chatStream(request)) {
        delivered.push(event);
        if (ending === "cancelled" && delivered.length === 2) break;
      }
      expect(delivered[0].toolCallDelta.text).toBe("synthetic-generated-tool-plan-");
      const spans = captured.getFinishedSpans();
      expect(spans).toHaveLength(1);
      const attrs = spans[0].attributes;
      expect(spans[0].status.code).toBe(ending === "terminal" ? SpanStatusCode.OK : ending === "cancelled" ? SpanStatusCode.UNSET : SpanStatusCode.ERROR);
      if (hidden) {
        expect(attrs[S.LLM_OUTPUT_MESSAGES]).toBeUndefined();
        expect(attrs[S.RAW_OUTPUT]).toBeUndefined();
        expect(JSON.stringify(attrs)).not.toContain("synthetic-generated-");
      } else {
        expect(JSON.parse(attrs[S.RAW_OUTPUT] as string).message.toolPlan).toBe(partial);
        expect(JSON.parse(attrs[S.LLM_OUTPUT_MESSAGES] as string)[0].tool_plan).toBe(partial);
      }
    });
  });

  it.each(["cancelled", "eof", "terminal", "generation"])("preserves v1 tool chunks (%s)", async (ending) => {
    await captureOutput(async (captured) => {
      const client = clientFor(sdk8);
      // Interleave calls and leave one argument incomplete: telemetry must not
      // require JSON parsing, mix call indices, or quote an already-raw string.
      const events: any[] = [
        { event_type: "tool-calls-chunk", tool_call_delta: { index: 1, name: "second", parameters: '{"synthetic-generated-second":' } },
        { event_type: "tool-calls-chunk", tool_call_delta: { index: 0, name: "lookup", parameters: '{"synthetic-generated-key":' } },
        { event_type: "tool-calls-chunk", tool_call_delta: { index: 0, parameters: '"value"}' } },
      ];
      const finalCalls = [{ name: "authoritative", parameters: { "synthetic-generated-final": true } }];
      if (ending === "terminal") events.push({ event_type: "stream-end", finish_reason: "COMPLETE", response: { text: "", tool_calls: finalCalls } });
      if (ending === "generation") events.push({ event_type: "tool-calls-generation", tool_calls: finalCalls });
      contentType = "application/x-ndjson";
      response = events.map(event => `${JSON.stringify(event)}\n`).join("");
      const delivered = [];
      for await (const event of await client.chatStream(request)) {
        delivered.push(event);
        if (ending === "cancelled" && delivered.length === 3) break;
      }
      expect(delivered[1].toolCallDelta.name).toBe("lookup");
      expect(delivered[2].toolCallDelta.parameters).toBe('"value"}');
      expect(delivered).toHaveLength(events.length);
      const spans = captured.getFinishedSpans();
      expect(spans).toHaveLength(1);
      const attrs = spans[0].attributes;
      expect(spans[0].status.code).toBe(ending === "terminal" ? SpanStatusCode.OK : ending === "cancelled" ? SpanStatusCode.UNSET : SpanStatusCode.ERROR);
      if (hidden) {
        expect(attrs[S.LLM_OUTPUT_MESSAGES]).toBeUndefined();
        expect(attrs[S.RAW_OUTPUT]).toBeUndefined();
        expect(JSON.stringify(attrs)).not.toContain("synthetic-generated-");
      } else {
        const calls = ["terminal", "generation"].includes(ending) ? finalCalls : [
          { name: "lookup", parameters: '{"synthetic-generated-key":"value"}' },
          { name: "second", parameters: '{"synthetic-generated-second":' },
        ];
        expect(JSON.parse(attrs[S.RAW_OUTPUT] as string).toolCalls).toEqual(calls);
        expect(JSON.parse(attrs[S.LLM_OUTPUT_MESSAGES] as string)[0].tool_calls).toEqual(calls.map(call => ({
          function: { name: call.name, arguments: typeof call.parameters === "string" ? call.parameters : JSON.stringify(call.parameters) },
        })));
      }
    });
  });
});
