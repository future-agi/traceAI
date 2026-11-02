import { SpanStatusCode, trace } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  ReadableSpan,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { OpenAIInstrumentation } from "@traceai/openai";
import { FireworksInstrumentation, isPatched } from "..";

// Use the real SDK; matrix jobs may provide a separately installed version.
const sdk = require(process.env.OPENAI_TEST_MODULE || "openai");
const OpenAI = sdk.OpenAI;

const FIREWORKS = "https://api.fireworks.ai/inference/v1";
const OPENAI = "https://api.openai.com/v1";
const SECRET = "SYNTHETIC-PRIVATE-CONTENT";
type Operation = "chat" | "completions" | "embeddings";
type Outcome = "success" | "error" | "stream";

function payload(operation: Operation) {
  if (operation === "embeddings") {
    return { object: "list", model: "returned-model", data: [{ index: 0, object: "embedding", embedding: [0.1] }], usage: { prompt_tokens: 2, total_tokens: 2 } };
  }
  return {
    id: "synthetic-id",
    created: 1,
    model: "returned-model",
    object: operation === "chat" ? "chat.completion" : "text_completion",
    choices: [{ index: 0, message: { role: "assistant", content: SECRET }, text: SECRET, finish_reason: "stop" }],
    usage: { prompt_tokens: 2, completion_tokens: 3, total_tokens: 5 },
  };
}

function chunk(operation: Operation) {
  return {
    id: "synthetic-id",
    created: 1,
    model: "returned-model",
    object: operation === "chat" ? "chat.completion.chunk" : "text_completion",
    choices: [{ index: 0, delta: { role: "assistant", content: SECRET }, text: SECRET, finish_reason: "stop" }],
  };
}

// The configured host stays real, so provider attribution is exercised, but the
// transport never leaves the process.
function client(baseURL: string, outcome: Outcome, operation: Operation) {
  return new OpenAI({
    apiKey: "synthetic-unused-key",
    baseURL,
    maxRetries: 0,
    fetch: async () => {
      if (outcome === "error") {
        return new Response(JSON.stringify({ error: { message: "synthetic failure", type: "invalid_request_error" } }), {
          status: 400,
          headers: { "content-type": "application/json" },
        });
      }
      if (outcome === "stream") {
        return new Response(`data: ${JSON.stringify(chunk(operation))}\n\ndata: [DONE]\n\n`, {
          headers: { "content-type": "text/event-stream" },
        });
      }
      return new Response(JSON.stringify(payload(operation)), { headers: { "content-type": "application/json" } });
    },
  });
}

async function call(baseURL: string, operation: Operation, outcome: Outcome) {
  const c = client(baseURL, outcome, operation);
  const stream = outcome === "stream" ? { stream: true } : {};
  const request =
    operation === "chat"
      ? c.chat.completions.create({ model: "accounts/fireworks/models/synthetic", messages: [{ role: "user", content: SECRET }], ...stream })
      : operation === "completions"
        ? c.completions.create({ model: "accounts/fireworks/models/synthetic", prompt: SECRET, ...stream })
        : c.embeddings.create({ model: "nomic-ai/synthetic", input: "synthetic embedding", encoding_format: "float" });
  if (outcome === "error") {
    await expect(request).rejects.toThrow();
    return;
  }
  const result = await request;
  if (outcome === "stream") {
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    for await (const _ of result) {
      // Consume the stream so its span ends.
    }
  }
}

const attr = (span: ReadableSpan, key: string) => span.attributes[key];

describe("FireworksInstrumentation (deprecated shared-route delegate)", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  let instrumentations: { disable(): void }[] = [];
  let emitWarning: jest.SpyInstance;

  beforeAll(() => {
    // The documented setup registers the provider before constructing instrumentation.
    trace.setGlobalTracerProvider(provider);
  });

  beforeEach(() => {
    exporter.reset();
    emitWarning = jest.spyOn(process, "emitWarning").mockImplementation(() => undefined);
  });

  afterEach(() => {
    for (const instrumentation of instrumentations.reverse()) instrumentation.disable();
    instrumentations = [];
    emitWarning.mockRestore();
  });

  afterAll(async () => {
    await provider.shutdown();
    trace.disable();
  });

  function fireworks(traceConfig?: ConstructorParameters<typeof FireworksInstrumentation>[0]) {
    const instrumentation = new FireworksInstrumentation(traceConfig);
    instrumentation.setTracerProvider(provider);
    instrumentation.manuallyInstrument(sdk);
    instrumentations.push(instrumentation);
    return instrumentation;
  }

  function shared() {
    const instrumentation = new OpenAIInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.manuallyInstrument(sdk);
    instrumentations.push(instrumentation);
    return instrumentation;
  }

  const cases: [Operation, Outcome][] = [
    ["chat", "success"],
    ["chat", "stream"],
    ["chat", "error"],
    ["completions", "success"],
    ["completions", "stream"],
    ["completions", "error"],
    ["embeddings", "success"],
    ["embeddings", "error"],
  ];

  it.each(cases)("exports one Fireworks-attributed span for %s %s", async (operation, outcome) => {
    fireworks();
    await call(FIREWORKS, operation, outcome);
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(attr(spans[0], "gen_ai.provider.name")).toBe("fireworks");
    if (outcome === "error") expect(spans[0].status.code).toBe(SpanStatusCode.ERROR);
    else expect(spans[0].status.code).not.toBe(SpanStatusCode.ERROR);
    expect(isPatched()).toBe(true);
  });

  it("attributes each client in a mixed process by its own host", async () => {
    fireworks();
    await call(FIREWORKS, "chat", "success");
    await call(OPENAI, "chat", "success");
    expect(exporter.getFinishedSpans().map((span) => attr(span, "gen_ai.provider.name"))).toEqual(["fireworks", "openai"]);
  });

  it("produces exactly one span when the shared instrumentor is also enabled", async () => {
    fireworks();
    shared();
    await call(FIREWORKS, "chat", "stream");
    await call(FIREWORKS, "embeddings", "success");
    expect(exporter.getFinishedSpans().map((span) => attr(span, "gen_ai.provider.name"))).toEqual(["fireworks", "fireworks"]);
  });

  it("keeps tracing when the instrumentor that did not patch is disabled", async () => {
    fireworks();
    const second = shared();
    second.disable();
    await call(FIREWORKS, "chat", "success");
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it("stops tracing after disable", async () => {
    const instrumentation = fireworks();
    instrumentation.disable();
    expect(isPatched()).toBe(false);
    await call(FIREWORKS, "chat", "success");
    expect(exporter.getFinishedSpans()).toHaveLength(0);
  });

  it("honors privacy options when the provider was registered first", async () => {
    fireworks({ traceConfig: { hideInputs: true, hideOutputs: true, hideErrorDetails: true } });
    await call(FIREWORKS, "chat", "success");
    await call(FIREWORKS, "chat", "error");
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(2);
    expect(JSON.stringify(spans.map((span) => [span.attributes, span.events, span.status]))).not.toMatch(
      /SYNTHETIC-PRIVATE-CONTENT|synthetic failure|synthetic-unused-key/,
    );
  });

  it("does not export the configured endpoint URL or credentials", async () => {
    fireworks();
    await call("https://url-user:url-password@api.fireworks.ai/inference/v1?query-secret=value", "chat", "success");
    const spans = exporter.getFinishedSpans();
    expect(attr(spans[0], "gen_ai.provider.name")).toBe("fireworks");
    expect(JSON.stringify(spans.map((span) => span.attributes))).not.toMatch(/url-user|url-password|query-secret|synthetic-unused-key/);
  });

  it("does not treat look-alike hosts as Fireworks", async () => {
    fireworks();
    await call("https://api.fireworks.ai.evil.test/inference/v1", "chat", "success");
    await call("https://fireworks.example/inference/v1", "chat", "success");
    expect(exporter.getFinishedSpans().map((span) => attr(span, "gen_ai.provider.name"))).toEqual(["openai", "openai"]);
  });

  it("emits one deprecation warning per process", () => {
    jest.isolateModules(() => {
      const { FireworksInstrumentation: Fresh } = require("..");
      instrumentations.push(new Fresh(), new Fresh());
    });
    const deprecations = emitWarning.mock.calls.filter(([, options]) => options?.type === "DeprecationWarning");
    expect(deprecations).toHaveLength(1);
    expect(String(deprecations[0][0])).toContain("@traceai/openai");
  });
});
