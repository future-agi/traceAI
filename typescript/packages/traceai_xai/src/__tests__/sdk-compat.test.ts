import { XAIInstrumentation } from "../instrumentation";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";
import OpenAI from "openai";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { createServer } from "node:http";

// Use the installed SDK's real resource instances; mocked resource objects miss
// the SDK's actual prototype layout and would not detect a no-op patch.
describe("installed OpenAI SDK compatibility", () => {
  const exporter = new InMemorySpanExporter();
  const bodies: Buffer[] = [];
  const server = createServer((req, res) => {
    if (req.url !== "/v1/traces") {
      res.writeHead(404).end();
      return;
    }
    const chunks: Buffer[] = [];
    req.on("data", chunk => chunks.push(chunk));
    req.on("end", () => {
      bodies.push(Buffer.concat(chunks));
      res.writeHead(200).end();
    });
  });
  let provider: NodeTracerProvider;
  let instrumentation: XAIInstrumentation;

  beforeAll(async () => {
    await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    if (!address || typeof address === "string") throw new Error("Expected loopback server port");
    const wireExporter = new OTLPTraceExporter({ url: `http://127.0.0.1:${address.port}/v1/traces` });
    provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter), new SimpleSpanProcessor(wireExporter)] });
    instrumentation = new XAIInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
    instrumentation.manuallyInstrument(require("openai"));
  });

  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
    await new Promise<void>((resolve, reject) => server.close(err => err ? reject(err) : resolve()));
  });

  it("traces real chat and embedding resources, but not non-xAI clients", async () => {
    const fetch: typeof globalThis.fetch = async (_input, init) => {
      const path = String(_input);
      const body = path.includes("embeddings")
        ? { object: "list", model: "grok-embedding", data: [{ object: "embedding", index: 0, embedding: [0.1, 0.2] }], usage: { prompt_tokens: 2, total_tokens: 2 } }
        : { id: "chatcmpl-1", object: "chat.completion", created: 1, model: "grok-test", choices: [{ index: 0, message: { role: "assistant", content: "hello" }, finish_reason: "stop" }], usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 } };
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    };
    const client = new OpenAI({ apiKey: "synthetic", baseURL: "https://api.x.ai/v1", fetch });
    const other = new OpenAI({ apiKey: "synthetic", baseURL: "https://api.openai.com/v1", fetch });
    await client.chat.completions.create({ model: "grok-test", messages: [{ role: "user", content: "hello" }] });
    await client.embeddings.create({ model: "grok-embedding", input: "hello" });
    await other.chat.completions.create({ model: "gpt-test", messages: [{ role: "user", content: "hello" }] });
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(2);
    expect(spans.map(s => s.name)).toEqual(["xAI Chat Completions", "xAI Embeddings"]);
    expect(spans[0].attributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL]).toBe(3);
    expect(spans[1].attributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL]).toBe(2);
  });

  it("exports instrumented SDK calls over OTLP to a loopback receiver", async () => {
    await provider.forceFlush();
    const payload = Buffer.concat(bodies).toString("utf8");
    expect(payload).toContain("xAI Chat Completions");
    expect(payload).toContain("xAI Embeddings");
    expect(payload).toContain("grok-test");
    expect(payload).toContain("hello");
    expect(payload).not.toContain("gpt-test");
  });

  it("retains APIPromise response helpers and structured parse", async () => {
    const fetch: typeof globalThis.fetch = async () => new Response(JSON.stringify({
      id: "chat-parse", object: "chat.completion", created: 1, model: "grok-test",
      choices: [{ index: 0, message: { role: "assistant", content: '{"ok":true}' }, finish_reason: "stop" }],
    }), { status: 200, headers: { "content-type": "application/json", "x-request-id": "req-1" } });
    const client = new OpenAI({ apiKey: "synthetic", baseURL: "https://api.x.ai/v1", fetch });
    const promise = client.chat.completions.create({ model: "grok-test", messages: [{ role: "user", content: "hi" }] });
    expect(typeof promise.withResponse).toBe("function");
    expect(typeof promise.asResponse).toBe("function");
    expect((await promise.withResponse()).response.status).toBe(200);
    const count = exporter.getFinishedSpans().length;
    const embedding = client.embeddings.create({ model: "grok-embedding", input: "hi" });
    expect(typeof embedding.withResponse).toBe("function");
    expect((await embedding.asResponse()).status).toBe(200);
    expect(exporter.getFinishedSpans()).toHaveLength(count + 1);
    if (typeof client.chat.completions.parse === "function") {
      const parsed = await client.chat.completions.parse({ model: "grok-test", messages: [{ role: "user", content: "hi" }], response_format: { type: "json_object" } });
      expect(parsed.choices[0].message.content).toBe('{"ok":true}');
    }
  });

  it("redacts embedding text on the wire with hideInputs", async () => {
    const hidden = new XAIInstrumentation({ traceConfig: { hideInputs: true, hideOutputs: true } });
    const sdk = require("openai");
    // Manual instrumentation is not registered with the module-loader unpatch.
    (instrumentation as unknown as { unpatch: (sdk: unknown) => void }).unpatch(sdk);
    hidden.setTracerProvider(provider);
    hidden.enable();
    hidden.manuallyInstrument(sdk);
    bodies.length = 0;
    const fetch: typeof globalThis.fetch = async () => new Response(JSON.stringify({ object: "list", model: "grok-embedding", data: [{ object: "embedding", index: 0, embedding: [0.1] }] }), { status: 200, headers: { "content-type": "application/json" } });
    try {
      const client = new OpenAI({ apiKey: "synthetic", baseURL: "https://api.x.ai/v1", fetch });
      await client.embeddings.create({ model: "grok-embedding", input: "DO_NOT_EXPORT_INPUT" });
      await provider.forceFlush();
      expect(Buffer.concat(bodies).toString("utf8")).not.toContain("DO_NOT_EXPORT_INPUT");
    } finally {
      (hidden as unknown as { unpatch: (sdk: unknown) => void }).unpatch(sdk);
      instrumentation.manuallyInstrument(sdk);
    }
  });

  it("aggregates streamed tool fragments and ends on early break", async () => {
    const chunks = [
      { id: "stream-1", model: "grok-test", choices: [{ index: 0, delta: { role: "assistant", tool_calls: [{ index: 0, id: "call_1", type: "function", function: { name: "weather", arguments: '{"city":' } }] }, finish_reason: null }] },
      { id: "stream-1", model: "grok-test", choices: [{ index: 0, delta: { tool_calls: [{ index: 0, function: { arguments: '"Paris"}' } }] }, finish_reason: "tool_calls" }] },
      { id: "stream-1", model: "grok-test", choices: [], usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 } },
    ];
    const fetch: typeof globalThis.fetch = async () => new Response(chunks.map(c => `data: ${JSON.stringify(c)}\n\n`).join("") + "data: [DONE]\n\n", { status: 200, headers: { "content-type": "text/event-stream" } });
    const client = new OpenAI({ apiKey: "synthetic", baseURL: "https://api.x.ai/v1", fetch });
    const stream = await client.chat.completions.create({ model: "grok-test", messages: [{ role: "user", content: "weather" }], stream: true });
    for await (const _chunk of stream) { /* consume */ }
    const finished = exporter.getFinishedSpans();
    const attrs = finished[finished.length - 1]?.attributes;
    expect(attrs?.[SemanticConventions.GEN_AI_RESPONSE_ID]).toBe("stream-1");
    expect(attrs?.[SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS]).toBe('["tool_calls"]');
    expect(attrs?.[SemanticConventions.LLM_TOKEN_COUNT_TOTAL]).toBe(3);
    expect(JSON.parse(attrs?.[SemanticConventions.LLM_OUTPUT_MESSAGES] as string)[0].tool_calls[0]).toMatchObject({ id: "call_1", function: { name: "weather", arguments: '{"city":"Paris"}' } });
    const count = exporter.getFinishedSpans().length;
    let cancelled = false;
    const cancellableFetch: typeof globalThis.fetch = async () => new Response(new ReadableStream({
      start(controller) { controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(chunks[0])}\n\n`)); },
      cancel() { cancelled = true; },
    }), { status: 200, headers: { "content-type": "text/event-stream" } });
    const earlyClient = new OpenAI({ apiKey: "synthetic", baseURL: "https://api.x.ai/v1", fetch: cancellableFetch });
    const early = await earlyClient.chat.completions.create({ model: "grok-test", messages: [{ role: "user", content: "weather" }], stream: true });
    for await (const _chunk of early) { break; }
    await provider.forceFlush();
    expect(cancelled).toBe(true);
    expect(exporter.getFinishedSpans()).toHaveLength(count + 1);
    expect(Buffer.concat(bodies).toString("utf8")).toContain("stream-1");
  });
});
