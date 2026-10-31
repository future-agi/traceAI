import http from "http";
import { AddressInfo } from "net";
import OpenAI, { ClientOptions } from "openai";
import { SpanStatusCode, trace } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { OpenAIInstrumentation } from "../instrumentation";

const BASE_URLS = [
  "https://api.cloudflare.com/client/v4/accounts/account-private/ai/v1",
  "https://gateway.ai.cloudflare.com/v1/account-private/gateway-private/compat",
];
const TOKEN = "synthetic-private-token";
const PROMPT = "synthetic-private-prompt";
const RESPONSE_MODEL = "claude-test";
const usage = { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 };
const chat = {
  id: "chat-1", object: "chat.completion", created: 1, model: RESPONSE_MODEL,
  choices: [{ index: 0, message: { role: "assistant", content: PROMPT }, finish_reason: "stop" }],
  usage,
};
const completion = {
  id: "completion-1", object: "text_completion", created: 1, model: RESPONSE_MODEL,
  choices: [{ index: 0, text: PROMPT, finish_reason: "stop", logprobs: null }], usage,
};
const response = {
  id: "response-1", object: "response", status: "completed", model: RESPONSE_MODEL,
  output: [{ type: "message", role: "assistant", content: [{ type: "output_text", text: PROMPT }] }],
  usage: { input_tokens: 3, output_tokens: 2, total_tokens: 5 },
};

type Operation = "chat" | "completion" | "response";
type Mode = "success" | "stream" | "error" | "stream-error";

// Uses the real SDK's request, response and SSE parsing, but never opens a vendor connection.
function fixtureFetch(operation: Operation, mode: Mode): NonNullable<ClientOptions["fetch"]> {
  return (async () => {
    if (mode === "error") {
      return new globalThis.Response(JSON.stringify({ error: { message: PROMPT, type: "invalid_request_error" } }), {
        status: 400, headers: { "content-type": "application/json" },
      });
    }
    if (mode === "success") {
      return new globalThis.Response(JSON.stringify(operation === "chat" ? chat : operation === "completion" ? completion : response), {
        headers: { "content-type": "application/json" },
      });
    }
    if (mode === "stream-error") {
      return new globalThis.Response(new ReadableStream({ pull(controller) { controller.error(new Error(PROMPT)); } }), {
        headers: { "content-type": "text/event-stream" },
      });
    }
    const event = operation === "chat" ? {
      ...chat, object: "chat.completion.chunk",
      choices: [{ index: 0, delta: { role: "assistant", content: PROMPT }, finish_reason: "stop" }],
    } : operation === "completion" ? completion : { type: "response.completed", response };
    const prefix = operation === "response" ? "event: response.completed\n" : "";
    return new globalThis.Response(`${prefix}data: ${JSON.stringify(event)}\n\ndata: [DONE]\n\n`, {
      headers: { "content-type": "text/event-stream" },
    });
  // SDK 4 uses node-fetch types; its transport also accepts the standard Response.
  }) as unknown as NonNullable<ClientOptions["fetch"]>;
}

async function call(client: OpenAI, operation: Operation, model: string, mode: Mode) {
  const stream = mode === "stream" || mode === "stream-error";
  const result = operation === "chat"
    ? await client.chat.completions.create({ model, messages: [{ role: "user", content: PROMPT }], stream })
    : operation === "completion"
      ? await client.completions.create({ model, prompt: PROMPT, stream })
      : await client.responses.create({ model, input: PROMPT, stream });
  if (stream) {
    for await (const _chunk of result as AsyncIterable<unknown>) { /* drain */ }
  }
  return result;
}

describe("provider attribution through the real OpenAI SDK", () => {
  const memory = new InMemorySpanExporter();
  const wires: Buffer[] = [];
  let server: http.Server;
  let provider: BasicTracerProvider;
  let instrumentation: OpenAIInstrumentation;

  beforeAll(async () => {
    server = http.createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on("data", (chunk) => chunks.push(chunk));
      req.on("end", () => {
        wires.push(Buffer.concat(chunks));
        res.end();
      });
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const { port } = server.address() as AddressInfo;
    provider = new BasicTracerProvider({ spanProcessors: [
      new SimpleSpanProcessor(memory),
      new SimpleSpanProcessor(new OTLPTraceExporter({ url: `http://127.0.0.1:${port}/v1/traces` })),
    ] });
    // register() makes the provider global before instrumentation construction.
    trace.setGlobalTracerProvider(provider);
    instrumentation = new OpenAIInstrumentation({ traceConfig: { hideInputs: true, hideOutputs: true } });
    instrumentation.setTracerProvider(provider);
    instrumentation.manuallyInstrument(OpenAI);
  });
  beforeEach(() => { memory.reset(); wires.length = 0; });
  afterAll(async () => {
    instrumentation.disable();
    trace.disable();
    await provider.shutdown();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });

  // OpenAI 4.0.0 predates Responses; exercise the APIs present in each SDK.
  const operations: Operation[] = OpenAI.Responses ? ["chat", "completion", "response"] : ["chat", "completion"];
  const modes: Mode[] = ["success", "stream", "error", "stream-error"];
  describe.each(BASE_URLS)("%s", (baseURL) => {
    test.each(operations.flatMap((operation) => modes.map((mode) => [operation, mode] as const)))(
      "%s %s attributes the explicit route, not the SDK or response model", async (operation, mode) => {
        const client = new OpenAI({ apiKey: TOKEN, baseURL, maxRetries: 0, fetch: fixtureFetch(operation, mode) });
        const pending = call(client, operation, "anthropic/claude-test", mode);
        if (mode === "error" || mode === "stream-error") await expect(pending).rejects.toThrow();
        else await pending;
        // Background stream readers resolve after the application's iterator.
        await new Promise<void>((resolve) => setImmediate(resolve));
        await provider.forceFlush();
        const spans = memory.getFinishedSpans();
        expect(spans).toHaveLength(1);
        expect(spans[0].attributes["gen_ai.provider.name"]).toBe("anthropic");
        expect(spans[0].attributes["server.address"]).toBe(new URL(baseURL).hostname);
        if (mode === "error" || mode === "stream-error") expect(spans[0].status.code).toBe(SpanStatusCode.ERROR);
        else expect(spans[0].status.code).not.toBe(SpanStatusCode.ERROR);
        expect(wires).toHaveLength(1);
        // The actual uncompressed OTLP/protobuf bytes include these string attributes.
        const wire = Buffer.concat(wires).toString("utf8");
        expect(wire).toContain("gen_ai.provider.name");
        expect(wire).toContain("anthropic");
        expect(wire).toContain(new URL(baseURL).hostname);
        for (const marker of [PROMPT, TOKEN, "account-private", "gateway-private"]) expect(wire).not.toContain(marker);
      },
    );
  });

  test.each([
    ["anthropic/claude-test", "anthropic"],
    ["openai/gpt-test", "openai"],
    ["google/gemini-test", "google"],
    ["google-ai-studio/gemini-test", "google"],
    ["xai/grok-test", "xai"],
    ["grok/grok-test", "xai"],
    ["@cf/meta/llama-test", "cloudflare"],
    ["workers-ai/@cf/meta/llama-test", "cloudflare"],
    ["dynamic/private-route", undefined],
    ["private-alias", undefined],
    ["unknown/model", undefined],
  ])("attributes %s without guessing dynamic aliases", async (model, expected) => {
    const client = new OpenAI({ apiKey: TOKEN, baseURL: BASE_URLS[0], fetch: fixtureFetch("chat", "success") });
    await call(client, "chat", model, "success");
    await provider.forceFlush();
    expect(memory.getFinishedSpans()).toHaveLength(1);
    expect(memory.getFinishedSpans()[0].attributes["gen_ai.provider.name"]).toBe(expected);
    expect(memory.getFinishedSpans()[0].attributes["server.address"]).toBe("api.cloudflare.com");
  });

  it("does not cache provider attribution across calls on one client", async () => {
    const client = new OpenAI({ apiKey: TOKEN, baseURL: BASE_URLS[1], fetch: fixtureFetch("chat", "success") });
    for (const model of ["anthropic/claude-test", "openai/gpt-test", "dynamic/route"]) await call(client, "chat", model, "success");
    await provider.forceFlush();
    expect(memory.getFinishedSpans().map((span) => span.attributes["gen_ai.provider.name"])).toEqual(["anthropic", "openai", undefined]);
  });
});
