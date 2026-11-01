import { XAIInstrumentation } from "../instrumentation";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { createServer } from "node:http";

describe("standard registration privacy defaults", () => {
  const wire: string[] = [];
  const receiver = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", chunk => chunks.push(chunk));
    req.on("end", () => { wire.push(Buffer.concat(chunks).toString("utf8")); res.writeHead(200).end(); });
  });
  let provider: NodeTracerProvider;
  let instrument: XAIInstrumentation;
  let unregister: () => void;
  const originalInputs = process.env.FI_HIDE_INPUTS;
  const originalOutputs = process.env.FI_HIDE_OUTPUTS;

  beforeAll(async () => {
    process.env.FI_HIDE_INPUTS = "true";
    process.env.FI_HIDE_OUTPUTS = "true";
    await new Promise<void>(resolve => receiver.listen(0, "127.0.0.1", resolve));
    const address = receiver.address();
    if (!address || typeof address === "string") throw new Error("Expected loopback port");
    provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(new OTLPTraceExporter({ url: `http://127.0.0.1:${address.port}/v1/traces` }))] });
    provider.register();
    instrument = new XAIInstrumentation();
    unregister = registerInstrumentations({ tracerProvider: provider, instrumentations: [instrument] });
  });

  afterAll(async () => {
    unregister();
    await provider.shutdown();
    await new Promise<void>((resolve, reject) => receiver.close(err => err ? reject(err) : resolve()));
    if (originalInputs === undefined) delete process.env.FI_HIDE_INPUTS;
    else process.env.FI_HIDE_INPUTS = originalInputs;
    if (originalOutputs === undefined) delete process.env.FI_HIDE_OUTPUTS;
    else process.env.FI_HIDE_OUTPUTS = originalOutputs;
  });

  it("hides embedding text when enabled by the superclass and registered normally", async () => {
    // Jest's module loader bypasses require-in-the-middle; patch the real SDK explicitly.
    instrument.manuallyInstrument(require("openai"));
    const OpenAI = require("openai").OpenAI as typeof import("openai").default;
    const client = new OpenAI({ apiKey: "synthetic", baseURL: "https://api.x.ai/v1", fetch: async () => new Response(JSON.stringify({ object: "list", model: "grok-embedding", data: [{ object: "embedding", index: 0, embedding: [0.5] }] }), { headers: { "content-type": "application/json" } }) });
    expect(instrument.isEnabled()).toBe(true);
    await client.embeddings.create({ model: "grok-embedding", input: "PRIVATE_EMBEDDING" });
    await provider.forceFlush();
    expect(wire.join(" ")).toContain("xAI Embeddings");
    expect(wire.join(" ")).not.toContain("PRIVATE_EMBEDDING");
  });

  it("honors explicit hideInputs and hideOutputs without a second enable", async () => {
    unregister();
    delete process.env.FI_HIDE_INPUTS;
    delete process.env.FI_HIDE_OUTPUTS;
    instrument = new XAIInstrumentation({ traceConfig: { hideInputs: true, hideOutputs: true } });
    unregister = registerInstrumentations({ tracerProvider: provider, instrumentations: [instrument] });
    // Jest's module loader bypasses require-in-the-middle; patch the real SDK explicitly.
    instrument.manuallyInstrument(require("openai"));
    const OpenAI = require("openai").OpenAI as typeof import("openai").default;
    const client = new OpenAI({ apiKey: "synthetic", baseURL: "https://api.x.ai/v1", fetch: async () => new Response(JSON.stringify({ id: "chat-private", object: "chat.completion", model: "grok-test", choices: [{ index: 0, message: { role: "assistant", content: "PRIVATE_OUTPUT" }, finish_reason: "stop" }] }), { headers: { "content-type": "application/json" } }) });
    wire.length = 0;
    await client.chat.completions.create({ model: "grok-test", messages: [{ role: "user", content: "PRIVATE_INPUT" }] });
    await provider.forceFlush();
    expect(wire.join(" ")).toContain("xAI Chat Completions");
    expect(wire.join(" ")).not.toMatch(/PRIVATE_INPUT|PRIVATE_OUTPUT/);
  });
});
