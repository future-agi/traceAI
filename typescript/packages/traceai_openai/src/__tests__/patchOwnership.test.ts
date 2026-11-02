import type { InstrumentationModuleDefinition } from "@opentelemetry/instrumentation";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OpenAIInstrumentation, isPatched } from "../instrumentation";

// Use the real SDK; matrix jobs may provide a separately installed version.
const sdk = require(process.env.OPENAI_TEST_MODULE || "openai");

// Jest's module registry bypasses require-in-the-middle, so record and patch the
// exports exactly as the CommonJS hook does; disable() later unpatches them.
function loadThroughRequireHook(instrumentation: OpenAIInstrumentation) {
  const [definition] = (instrumentation as unknown as { _modules: InstrumentationModuleDefinition[] })._modules;
  definition.moduleExports = sdk;
  definition.patch!(sdk, sdk.VERSION);
}

describe("instrumentors sharing one SDK module", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const client = new sdk.OpenAI({
    apiKey: "synthetic-unused-key",
    baseURL: "https://api.fireworks.ai/inference/v1",
    maxRetries: 0,
    fetch: async () => new Response(
      JSON.stringify({ id: "synthetic-id", object: "chat.completion", created: 1, model: "m", choices: [] }),
      { headers: { "content-type": "application/json" } },
    ),
  });
  const chat = () => client.chat.completions.create({ model: "m", messages: [{ role: "user", content: "Hello" }] });

  afterAll(() => provider.shutdown());
  beforeEach(() => exporter.reset());

  it("keeps the owner's patch when an instrumentor that did not patch is disabled", async () => {
    const owner = new OpenAIInstrumentation();
    const other = new OpenAIInstrumentation();
    owner.setTracerProvider(provider);
    other.setTracerProvider(provider);
    loadThroughRequireHook(owner);
    loadThroughRequireHook(other);

    other.disable();
    expect(isPatched()).toBe(true);
    await chat();
    expect(exporter.getFinishedSpans()).toHaveLength(1);

    owner.disable();
    expect(isPatched()).toBe(false);
    await chat();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });
});
