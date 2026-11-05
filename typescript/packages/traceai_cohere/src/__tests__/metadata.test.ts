import { createServer, Server } from "node:http";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { SpanStatusCode } from "@opentelemetry/api";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import { TraceConfigOptions } from "@traceai/fi-core";
import { CohereInstrumentation } from "../instrumentation";

// Real SDK serialization/iteration against an offline HTTP server. No provider keys.
const sdk = require("cohere-ai");
const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
let server: Server;
let url: string;
let response: unknown;
let streaming = false;
let wire: any[] = [];
let instrumentation: CohereInstrumentation;
const usage = { cached_tokens: 11, tokens: { input_tokens: 42, output_tokens: 9 }, billed_units: { input_tokens: 30, output_tokens: 7 } };
const request = { model: "command-fixture", messages: [{ role: "user", content: "private-input" }] };
const generationId = "synthetic-generation-19";
const citation = {
  start: 0, end: 8, text: "Evidence", type: "TEXT_CONTENT",
  sources: [
    { type: "document", id: "doc-19", document: { text: "private-input" } },
    { type: "tool", id: "tool-19", tool_output: { text: "private-input" } },
  ],
};
const start = { type: "message-start", id: generationId, delta: { message: { role: "assistant" } } };
const content = { type: "content-delta", index: 0, delta: { message: { content: { text: "Evidence" } } } };
const terminal = { type: "message-end", delta: { finish_reason: "COMPLETE", usage } };
function events(items: unknown[], invalidTail = false) {
  streaming = true;
  response = items.map(item => `data: ${JSON.stringify(item)}\n\n`).join("") + (invalidTail ? "data: not-json\n\n" : "");
}
function client(config: TraceConfigOptions = {}, v2 = true) {
  instrumentation = new CohereInstrumentation({ traceConfig: config });
  instrumentation.setTracerProvider(provider);
  instrumentation.manuallyInstrument(sdk);
  return new (v2 ? sdk.CohereClientV2 : sdk.CohereClient)({ token: "offline-placeholder", environment: url });
}
function attributes() {
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(1);
  return spans[0].attributes;
}

beforeAll(async () => {
  server = createServer((req, reply) => {
    const chunks: Buffer[] = [];
    req.on("data", chunk => chunks.push(chunk));
    req.on("end", () => {
      if (req.url === "/v1/traces") {
        wire.push(JSON.parse(Buffer.concat(chunks).toString()));
        reply.setHeader("Content-Type", "application/json");
        reply.end("{}");
        return;
      }
      reply.setHeader("Content-Type", streaming ? "text/event-stream" : "application/json");
      reply.end(typeof response === "string" ? response : JSON.stringify(response));
    });
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  url = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
});
beforeEach(() => { exporter.reset(); streaming = false; wire = []; });
afterEach(() => instrumentation?.disable());
afterAll(async () => {
  await new Promise<void>(resolve => server.close(() => resolve()));
  await provider.shutdown();
});

it.each([false, true])("preserves cached and separately billed tokens with hidden content (stream=%s)", async stream => {
  const c = client({ hideInputs: true, hideOutputs: true });
  if (stream) {
    events([start, content, terminal]);
    const delivered = [];
    for await (const event of await c.chatStream(request)) delivered.push(event);
    expect(delivered[2].delta.usage.cachedTokens).toBe(11);
    expect(delivered[2].delta.usage.billedUnits.inputTokens).toBe(30);
  } else {
    response = { id: generationId, message: { role: "assistant", content: [{ type: "text", text: "Evidence" }] }, usage };
    const result = await c.chat(request);
    expect(result.usage.cachedTokens).toBe(11);
    expect(result.usage.billedUnits.inputTokens).toBe(30);
  }
  expect(attributes()).toMatchObject({
    [S.LLM_TOKEN_COUNT_PROMPT]: 42,
    [S.LLM_TOKEN_COUNT_COMPLETION]: 9,
    [S.LLM_TOKEN_COUNT_TOTAL]: 51,
    [S.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ]: 11,
    "cohere.usage.billed_units.input_tokens": 30,
    "cohere.usage.billed_units.output_tokens": 7,
  });
  expect(attributes()[S.RAW_OUTPUT]).toBeUndefined();
  expect(JSON.stringify(attributes())).not.toMatch(/private-input|Evidence/);
});

it.each([false, true])("preserves billed search units with hidden rerank content (v2=%s)", async v2 => {
  const c = client({ hideInputs: true, hideOutputs: true }, v2);
  response = { id: "rerank-fixture", results: [{ index: 0, relevance_score: 0.9 }], meta: { billed_units: { search_units: 2 } } };
  const result = await c.rerank({ model: "rerank-fixture", query: "private-input", documents: ["private-input"] });
  expect(result.meta.billedUnits.searchUnits).toBe(2);
  expect(attributes()["cohere.usage.billed_units.search_units"]).toBe(2);
  expect(attributes()[S.LLM_TOKEN_COUNT_TOTAL]).toBeUndefined();
  expect(attributes()[S.RAW_OUTPUT]).toBeUndefined();
});

const privacyModes: TraceConfigOptions[] = [
  {}, { hideInputs: true }, { hideInputMessages: true }, { hideInputText: true }, { hideInputImages: true },
  { hideOutputs: true }, { hideOutputMessages: true }, { hideOutputText: true },
];
describe.each(privacyModes)("citation privacy %j", config => {
  it.each(["chat", "complete", "cancelled", "eof", "failure"])("retains indexed citation provenance on %s", async ending => {
    const c = client(config);
    const ragRequest = { ...request, documents: [{ id: "doc-19", data: { text: "private-input" } }] };
    const second = { ...citation, start: 9, end: 17, sources: citation.sources.slice(0, 1) };
    let deliveredCitation: unknown;
    if (ending === "chat") {
      response = { id: generationId, message: { role: "assistant", content: [{ type: "text", text: "Evidence" }], citations: [citation, second] } };
      deliveredCitation = (await c.chat(ragRequest)).message.citations[0];
    } else {
      const items: unknown[] = [start, content,
        { type: "citation-start", index: 1, delta: { message: { citations: second } } },
        { type: "citation-start", index: 0, delta: { message: { citations: citation } } },
      ];
      if (ending === "complete") items.push({ type: "citation-end", index: 0 }, { type: "citation-end", index: 1 }, terminal);
      events(items, ending === "failure");
      const consume = async () => {
        for await (const event of await c.chatStream(ragRequest)) {
          if (event.type === "citation-start" && event.index === 0) {
            deliveredCitation = event.delta.message.citations;
            if (ending === "cancelled") break;
          }
        }
      };
      if (ending === "failure") await expect(consume()).rejects.toBeInstanceOf(Error);
      else await consume();
    }
    // Telemetry projection must never mutate data delivered to the caller.
    expect(JSON.stringify(deliveredCitation)).toContain("private-input");
    const attrs = attributes();
    const outputHidden = config.hideOutputs || config.hideOutputMessages || config.hideOutputText;
    const inputHidden = config.hideInputs || config.hideInputMessages || config.hideInputText || config.hideInputImages;
    if (outputHidden) {
      expect(attrs[S.LLM_OUTPUT_MESSAGES]).toBeUndefined();
      expect(attrs[S.RAW_OUTPUT]).toBeUndefined();
    } else {
      const expected = [citation, second].map(item => inputHidden ? {
        start: item.start, end: item.end, text: item.text, type: item.type,
        sources: item.sources.map(source => ({ type: source.type, id: source.id })),
      } : {
        ...item,
        sources: item.sources.map(source => "tool_output" in source ? {
          type: source.type, id: source.id, toolOutput: source.tool_output,
        } : source),
      });
      expect(JSON.parse(attrs[S.LLM_OUTPUT_MESSAGES] as string)[0].citations).toEqual(expected);
      expect(JSON.parse(attrs[S.RAW_OUTPUT] as string).message.citations).toEqual(expected);
    }
    if (inputHidden) expect(JSON.stringify(attrs)).not.toContain("private-input");
    expect(exporter.getFinishedSpans()[0].status.code).toBe(
      ending === "cancelled" ? SpanStatusCode.UNSET : ["eof", "failure"].includes(ending) ? SpanStatusCode.ERROR : SpanStatusCode.OK,
    );
  });
});

it.each(["complete", "cancelled", "abort", "failure", "eof"])("retains the stream-start generation ID with hidden output on %s", async ending => {
  const c = client({ hideInputs: true, hideOutputs: true });
  events(ending === "complete" ? [start, content, terminal] : [start], ending === "failure");
  const stream = await c.chatStream(request);
  const consume = async () => {
    for await (const event of stream) {
      expect(event.type).toBeDefined();
      if (ending === "cancelled") break;
      if (ending === "abort") { stream.controller.abort(); break; }
    }
  };
  if (ending === "failure") await expect(consume()).rejects.toBeInstanceOf(Error);
  else await consume();
  expect(attributes()[S.GEN_AI_RESPONSE_ID]).toBe(generationId);
  expect(attributes()[S.RAW_OUTPUT]).toBeUndefined();
});

it("exports first generated-content latency for a real SDK stream even when content is hidden", async () => {
  const c = client({ hideInputs: true, hideOutputs: true });
  events([start, content, terminal]);
  for await (const _event of await c.chatStream(request)) { /* consume */ }
  const elapsed = attributes()["cohere.stream.time_to_first_token_ms"];
  expect(typeof elapsed).toBe("number");
  expect(elapsed).toBeGreaterThanOrEqual(0);
});

it.each([false, true])("exports independent metadata and citation privacy over OTLP/HTTP (hideOutputs=%s)", async hideOutputs => {
  const c = client({ hideInputs: true, hideOutputs });
  const wireProvider = new NodeTracerProvider({ spanProcessors: [
    new SimpleSpanProcessor(new OTLPTraceExporter({ url: `${url}/v1/traces` })),
  ] });
  instrumentation.setTracerProvider(wireProvider);
  try {
    events([start, content, { type: "citation-start", index: 0, delta: { message: { citations: citation } } }, terminal]);
    for await (const _event of await c.chatStream({ ...request, documents: [{ id: "doc-19", data: { text: "private-input" } }] })) { /* consume */ }
    streaming = false;
    response = { id: "rerank-fixture", results: [{ index: 0, relevance_score: 0.9 }], meta: { billed_units: { search_units: 2 } } };
    await c.rerank({ model: "rerank-fixture", query: "private-input", documents: ["private-input"] });
    await wireProvider.forceFlush();
    const spans = wire.flatMap(batch => batch.resourceSpans.flatMap((resource: any) => resource.scopeSpans.flatMap((scope: any) => scope.spans)));
    expect(spans).toHaveLength(2);
    const attrs = spans.map(span => Object.fromEntries(span.attributes.map((item: any) => [item.key, Object.values(item.value)[0]])));
    expect(attrs[0]).toMatchObject({
      [S.GEN_AI_RESPONSE_ID]: generationId, [S.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ]: 11,
      [S.LLM_TOKEN_COUNT_PROMPT]: 42, [S.LLM_TOKEN_COUNT_COMPLETION]: 9,
      "cohere.usage.billed_units.input_tokens": 30, "cohere.usage.billed_units.output_tokens": 7,
      "cohere.stream.time_to_first_token_ms": expect.any(Number),
    });
    expect(attrs[1]["cohere.usage.billed_units.search_units"]).toBe(2);
    expect(JSON.stringify(wire)).not.toContain("private-input");
    if (hideOutputs) {
      expect(attrs[0][S.RAW_OUTPUT]).toBeUndefined();
      expect(attrs[0][S.LLM_OUTPUT_MESSAGES]).toBeUndefined();
    } else {
      const sources = [{ type: "document", id: "doc-19" }, { type: "tool", id: "tool-19" }];
      expect(JSON.parse(attrs[0][S.RAW_OUTPUT]).message.citations[0].sources).toEqual(sources);
      expect(JSON.parse(attrs[0][S.LLM_OUTPUT_MESSAGES])[0].citations[0].sources).toEqual(sources);
    }
  } finally {
    await wireProvider.shutdown();
  }
});
