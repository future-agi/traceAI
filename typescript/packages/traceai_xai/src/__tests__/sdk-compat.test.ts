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
});
