/**
 * E2E tests for Fireworks AI instrumentation
 *
 * These tests require a valid FIREWORKS_API_KEY.
 *
 * Example:
 *   FIREWORKS_API_KEY=your_key npm test -- --testPathPattern=e2e
 */
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FireworksInstrumentation } from "../instrumentation";
import { SemanticConventions, FISpanKind, LLMProvider } from "@traceai/fi-semantic-conventions";

const FIREWORKS_API_KEY = process.env.FIREWORKS_API_KEY;

const describeIf = FIREWORKS_API_KEY ? describe : describe.skip;

describeIf("FireworksInstrumentation E2E", () => {
  let provider: NodeTracerProvider;
  let memoryExporter: InMemorySpanExporter;
  let instrumentation: FireworksInstrumentation;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let OpenAI: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let client: any;

  beforeAll(async () => {
    memoryExporter = new InMemorySpanExporter();
    provider = new NodeTracerProvider();
    provider.addSpanProcessor(new SimpleSpanProcessor(memoryExporter));
    provider.register();

    instrumentation = new FireworksInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const openaiModule = await import("openai");
    instrumentation.manuallyInstrument(openaiModule as unknown as Record<string, unknown>);
    OpenAI = openaiModule.default;

    client = new OpenAI({
      baseURL: "https://api.fireworks.ai/inference/v1",
      apiKey: FIREWORKS_API_KEY,
    });
  });

  afterAll(async () => {
    await provider.shutdown();
  });

  beforeEach(() => {
    memoryExporter.reset();
  });

  it("should trace chat completion", async () => {
    const response = await client.chat.completions.create({
      model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
      messages: [
        { role: "system", content: "You are a helpful assistant." },
        { role: "user", content: "Say hello in one word." },
      ],
      max_tokens: 10,
    });

    expect(response.choices[0].message.content).toBeDefined();

    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBe(1);

    const span = spans[0];
    expect(span.name).toBe("Fireworks Chat Completions");
    expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
    expect(span.attributes[SemanticConventions.LLM_PROVIDER]).toBe(LLMProvider.FIREWORKS);
    expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT]).toBeDefined();
    expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]).toBeDefined();
  }, 30000);

  it("should trace streaming chat completion", async () => {
    const stream = await client.chat.completions.create({
      model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
      messages: [
        { role: "user", content: "Count from 1 to 3." },
      ],
      max_tokens: 20,
      stream: true,
    });

    let fullContent = "";
    for await (const chunk of stream) {
      const content = chunk.choices[0]?.delta?.content;
      if (content) {
        fullContent += content;
      }
    }

    expect(fullContent.length).toBeGreaterThan(0);

    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBe(1);

    const span = spans[0];
    expect(span.name).toBe("Fireworks Chat Completions");
    expect(span.attributes[SemanticConventions.OUTPUT_VALUE]).toBe(fullContent);
  }, 30000);

  it("should trace text completion", async () => {
    const response = await client.completions.create({
      model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
      prompt: "The capital of France is",
      max_tokens: 10,
    });

    expect(response.choices[0].text).toBeDefined();

    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBe(1);

    const span = spans[0];
    expect(span.name).toBe("Fireworks Completions");
    expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
  }, 30000);

  it("should trace embeddings", async () => {
    const response = await client.embeddings.create({
      model: "nomic-ai/nomic-embed-text-v1.5",
      input: "Hello, world!",
    });

    expect(response.data[0].embedding.length).toBeGreaterThan(0);

    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBe(1);

    const span = spans[0];
    expect(span.name).toBe("Fireworks Embeddings");
    expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.EMBEDDING);
    expect(span.attributes[SemanticConventions.EMBEDDING_MODEL_NAME]).toBe("nomic-ai/nomic-embed-text-v1.5");
  }, 30000);

  it("should trace batch embeddings", async () => {
    const response = await client.embeddings.create({
      model: "nomic-ai/nomic-embed-text-v1.5",
      input: ["Hello", "World"],
    });

    expect(response.data.length).toBe(2);

    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBe(1);

    const span = spans[0];
    expect(span.attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_TEXT}`]).toBe("Hello");
    expect(span.attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.1.${SemanticConventions.EMBEDDING_TEXT}`]).toBe("World");
  }, 30000);

  it("should handle errors gracefully", async () => {
    await expect(
      client.chat.completions.create({
        model: "non-existent-model",
        messages: [{ role: "user", content: "Hello" }],
      })
    ).rejects.toThrow();

    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBe(1);
    expect(spans[0].status.code).toBe(2); // ERROR
  }, 30000);
});
