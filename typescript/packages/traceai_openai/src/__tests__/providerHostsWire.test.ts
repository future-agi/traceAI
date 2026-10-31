import http from "http";
import { AddressInfo } from "net";
import { trace } from "@opentelemetry/api";
import { BasicTracerProvider, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { OpenAIInstrumentation } from "../instrumentation";

// Use the real SDK; matrix jobs may provide a separately installed version.
const OpenAI = require(process.env.OPENAI_TEST_MODULE || "openai").OpenAI;
const operations = ["chat", "completions", "embeddings", ...(OpenAI.Responses ? ["responses"] : [])];
type Outcome = "success" | "error" | "stream" | "stream-error" | "cancel";
type WireSpan = {
  name: string;
  attributes: { key: string; value: { stringValue?: string } }[];
  status: { code?: number };
};

function payload(operation: string) {
  const common = { id: "synthetic-id", model: "returned-model", created: 1 };
  if (operation === "embeddings") return { object: "list", data: [{ index: 0, embedding: [0.1, 0.2] }] };
  if (operation === "responses") return { ...common, object: "response", status: "completed", output: [] };
  return {
    ...common,
    object: operation === "chat" ? "chat.completion" : "text_completion",
    choices: [{ index: 0, message: { role: "assistant", content: "Hello" }, text: "Hello", finish_reason: "stop" }],
  };
}

function streamPayload(operation: string) {
  if (operation === "responses") return { type: "response.completed", response: payload(operation) };
  return {
    ...payload(operation),
    object: operation === "chat" ? "chat.completion.chunk" : "text_completion",
    choices: [{ index: 0, delta: { role: "assistant", content: "Hello" }, text: "Hello", finish_reason: "stop" }],
  };
}

function create(client: any, operation: string, outcome: Outcome) {
  const params = { model: "requested-model", ...(outcome.includes("stream") || outcome === "cancel" ? { stream: true } : {}) };
  if (operation === "chat") return client.chat.completions.create({ ...params, messages: [{ role: "user", content: "Hello" }] });
  if (operation === "completions") return client.completions.create({ ...params, prompt: "Hello" });
  if (operation === "embeddings") return client.embeddings.create({ ...params, input: "Hello", encoding_format: "float" });
  return client.responses.create({ ...params, input: "Hello" });
}

describe("provider attribution on the OTLP wire", () => {
  let server: http.Server;
  let origin: string;
  let provider: BasicTracerProvider;
  let instrumentation: OpenAIInstrumentation;
  let spans: WireSpan[] = [];

  beforeAll(async () => {
    server = http.createServer(async (req, res) => {
      const parts: Buffer[] = [];
      for await (const part of req) parts.push(part);
      const raw = Buffer.concat(parts).toString();
      res.setHeader("content-type", "application/json");
      if (req.url === "/v1/traces") {
        const body = JSON.parse(raw);
        for (const resource of body.resourceSpans) {
          for (const scope of resource.scopeSpans) spans.push(...scope.spans);
        }
        res.end("{}");
        return;
      }
      const [, operation, outcome] = req.url!.split("/");
      if (outcome === "error") {
        res.statusCode = 400;
        res.end(JSON.stringify({ error: { message: "synthetic failure", type: "invalid_request_error" } }));
      } else if (outcome === "success") {
        res.end(JSON.stringify(payload(operation)));
      } else {
        res.setHeader("content-type", "text/event-stream");
        res.write(`data: ${JSON.stringify(streamPayload(operation))}\n\n`);
        // Broken SSE exercises errors during iteration, not just an HTTP rejection.
        res.end(outcome === "stream-error" ? "data: {invalid-json}\n\n" : "data: [DONE]\n\n");
      }
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    provider = new BasicTracerProvider({
      spanProcessors: [new SimpleSpanProcessor(new OTLPTraceExporter({ url: `${origin}/v1/traces`, headers: {} }))],
    });
    trace.setGlobalTracerProvider(provider);
    instrumentation = new OpenAIInstrumentation({
      traceConfig: { hideInputs: true, hideOutputs: true, hideErrorDetails: true },
    });
    instrumentation.setTracerProvider(provider);
    instrumentation.manuallyInstrument(OpenAI);
  });

  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
    trace.disable();
    await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  });
  beforeEach(() => { spans = []; });

  function client(baseURL: string, operation: string, outcome: Outcome) {
    return new OpenAI({
      apiKey: "synthetic-unused-key",
      baseURL,
      maxRetries: 0,
      // The configured host remains real; the transport can only reach localhost.
      fetch: (_url: unknown, options: RequestInit) => fetch(`${origin}/${operation}/${outcome}`, options),
    });
  }

  async function assertSpan(expected: string, error = false) {
    await provider.forceFlush();
    expect(spans).toHaveLength(1);
    expect(spans[0].attributes.find(({ key }) => key === "gen_ai.provider.name")?.value.stringValue).toBe(expected);
    if (error) expect(spans[0].status.code).toBe(2);
    expect(JSON.stringify(spans)).not.toMatch(/synthetic failure|synthetic-unused-key|url-user|url-password|query-secret/);
    // Embedding text has a separate, pre-existing fi-core masking gap. This
    // change does not claim hideInputs covers embedding.embeddings.*.text.
    if (spans[0].name !== "OpenAI Embeddings") {
      expect(JSON.stringify(spans)).not.toContain("Hello");
    }
  }

  for (const operation of operations) {
    const outcomes: Outcome[] = operation === "embeddings" ? ["success", "error"] : ["success", "error", "stream", "stream-error", "cancel"];
    for (const outcome of outcomes) {
      it(`${operation} ${outcome} preserves Anannas attribution`, async () => {
        const result = create(client("https://api.anannas.ai/v1", operation, outcome), operation, outcome);
        if (outcome === "error") await expect(result).rejects.toThrow();
        else if (outcome === "stream-error") {
          await expect((async () => { for await (const _chunk of await result) { /* consume */ } })()).rejects.toThrow();
        } else if (outcome === "stream" || outcome === "cancel") {
          let count = 0;
          for await (const chunk of await result) {
            expect(chunk).toEqual(streamPayload(operation));
            count++;
            if (outcome === "cancel") break;
          }
          expect(count).toBe(1);
        } else {
          expect(await result).toMatchObject(payload(operation));
        }
        await assertSpan("anannas", outcome === "error" || outcome === "stream-error");
      });
    }
  }

  for (const operation of operations.filter((operation) => operation !== "embeddings")) {
    it(`${operation} preserves the stream controller and tee helper when available`, async () => {
      const stream = await create(client("https://api.anannas.ai/v1", operation, "stream"), operation, "stream");
      expect(stream.controller).toBeInstanceOf(AbortController);
      const read = async (source: AsyncIterable<unknown>) => {
        const chunks = [];
        for await (const chunk of source) chunks.push(chunk);
        return chunks;
      };
      if (typeof stream.tee === "function") {
        const [left, right] = stream.tee();
        expect(await Promise.all([read(left), read(right)]))
          .toEqual([[streamPayload(operation)], [streamPayload(operation)]]);
      } else {
        // OpenAI 4.0 has no tee helper, but retains its original stream object.
        expect(await read(stream)).toEqual([streamPayload(operation)]);
      }
      await assertSpan("anannas");
    });
  }

  it.each([
    ["https://ANANNAS.ai./v1", "anannas"],
    ["https://API.ANANNAS.AI.:8443/v1/", "anannas"],
    ["https://url-user:url-password@api.anannas.ai/v1?query-secret=value", "anannas"],
    ["https://api.anannas.ai.evil.test/v1", "openai"],
    ["https://evil.test/api.anannas.ai", "openai"],
    ["https://api.anannas.ai@evil.test/v1", "openai"],
    ["https://proxy.example.test/v1", "openai"],
    ["http://localhost:1234/v1", "openai"],
    ["https://api.openai.com/v1", "openai"],
    ["https://resource.openai.azure.com/openai/deployments/test", "azure"],
    ["https://api.deepseek.com/v1", "deepseek"],
    ["https://api.x.ai/v1", "xai"],
  ])("normalizes %s without guessing", async (baseURL, expected) => {
    await create(client(baseURL, "chat", "success"), "chat", "success");
    await assertSpan(expected);
  });

  it("resolves per client and per call rather than caching the first host", async () => {
    const first = client("https://api.anannas.ai/v1", "chat", "success");
    const second = client("https://api.openai.com/v1", "chat", "success");
    await Promise.all([create(first, "chat", "success"), create(second, "chat", "success")]);
    first.baseURL = "https://api.deepseek.com/v1";
    await create(first, "chat", "success");
    await provider.forceFlush();
    expect(spans).toHaveLength(3);
    expect(spans.map((span) => span.attributes.find(({ key }) => key === "gen_ai.provider.name")?.value.stringValue).sort())
      .toEqual(["anannas", "deepseek", "openai"]);
  });
});
