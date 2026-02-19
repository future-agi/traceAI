/**
 * End-to-end tests for Together AI instrumentation.
 *
 * These tests require a valid TOGETHER_API_KEY environment variable.
 * Skip these tests in CI environments without API access.
 */
import { TogetherInstrumentation } from "../instrumentation";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SemanticConventions, FISpanKind, LLMSystem } from "@traceai/fi-semantic-conventions";

// Check if we have API access
const hasApiKey = !!process.env.TOGETHER_API_KEY;

// Use conditional describe
const describeE2E = hasApiKey ? describe : describe.skip;

describeE2E("TogetherInstrumentation E2E Tests", () => {
  let instrumentation: TogetherInstrumentation;
  let memoryExporter: InMemorySpanExporter;
  let provider: NodeTracerProvider;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let Together: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let client: any;

  beforeAll(async () => {
    // Set up OpenTelemetry
    memoryExporter = new InMemorySpanExporter();
    provider = new NodeTracerProvider();
    provider.addSpanProcessor(new SimpleSpanProcessor(memoryExporter));
    provider.register();

    // Create and enable instrumentation
    instrumentation = new TogetherInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    // Dynamically import together-ai
    const togetherModule = await import("together-ai");
    Together = togetherModule.Together;

    // Manually patch the module
    instrumentation.manuallyInstrument(togetherModule);

    // Create client
    client = new Together({
      apiKey: process.env.TOGETHER_API_KEY,
    });
  });

  afterAll(async () => {
    await provider.shutdown();
  });

  beforeEach(() => {
    memoryExporter.reset();
  });

  describe("chat.completions.create", () => {
    it("should instrument chat completions", async () => {
      const response = await client.chat.completions.create({
        model: "meta-llama/Llama-3-8b-chat-hf",
        messages: [
          { role: "user", content: "Say 'hello' and nothing else." },
        ],
        max_tokens: 10,
      });

      expect(response).toBeDefined();
      expect(response.choices).toBeDefined();
      expect(response.choices.length).toBeGreaterThan(0);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);

      const span = spans[0];
      expect(span.name).toBe("Together Chat Completions");
      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
      expect(span.attributes[SemanticConventions.LLM_SYSTEM]).toBe(LLMSystem.TOGETHER);
      expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBeDefined();
    }, 30000);

    it("should capture token usage", async () => {
      const response = await client.chat.completions.create({
        model: "meta-llama/Llama-3-8b-chat-hf",
        messages: [
          { role: "user", content: "Count to 3." },
        ],
        max_tokens: 20,
      });

      expect(response.usage).toBeDefined();

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT]).toBeDefined();
      expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]).toBeDefined();
      expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL]).toBeDefined();
    }, 30000);

    it("should handle streaming", async () => {
      const stream = await client.chat.completions.create({
        model: "meta-llama/Llama-3-8b-chat-hf",
        messages: [
          { role: "user", content: "Say 'hi'." },
        ],
        max_tokens: 5,
        stream: true,
      });

      const chunks: unknown[] = [];
      for await (const chunk of stream) {
        chunks.push(chunk);
      }

      expect(chunks.length).toBeGreaterThan(0);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);

      const span = spans[0];
      expect(span.attributes[SemanticConventions.OUTPUT_VALUE]).toBeDefined();
    }, 30000);

    it("should capture input/output messages", async () => {
      await client.chat.completions.create({
        model: "meta-llama/Llama-3-8b-chat-hf",
        messages: [
          { role: "system", content: "You are a helpful assistant." },
          { role: "user", content: "Hello!" },
        ],
        max_tokens: 10,
      });

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      // Check input messages
      expect(span.attributes[`${SemanticConventions.LLM_INPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_ROLE}`]).toBe("system");
      expect(span.attributes[`${SemanticConventions.LLM_INPUT_MESSAGES}.1.${SemanticConventions.MESSAGE_ROLE}`]).toBe("user");

      // Check output message
      expect(span.attributes[`${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_ROLE}`]).toBe("assistant");
    }, 30000);
  });

  describe("completions.create (legacy)", () => {
    it("should instrument completions", async () => {
      const response = await client.completions.create({
        model: "togethercomputer/RedPajama-INCITE-7B-Base",
        prompt: "The capital of France is",
        max_tokens: 10,
      });

      expect(response).toBeDefined();
      expect(response.choices).toBeDefined();

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);

      const span = spans[0];
      expect(span.name).toBe("Together Completions");
      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
    }, 30000);
  });

  describe("embeddings.create", () => {
    it("should instrument embeddings", async () => {
      const response = await client.embeddings.create({
        model: "togethercomputer/m2-bert-80M-8k-retrieval",
        input: "Hello world",
      });

      expect(response).toBeDefined();
      expect(response.data).toBeDefined();
      expect(response.data.length).toBeGreaterThan(0);
      expect(response.data[0].embedding).toBeDefined();

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);

      const span = spans[0];
      expect(span.name).toBe("Together Embeddings");
      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.EMBEDDING);
      expect(span.attributes[SemanticConventions.EMBEDDING_MODEL_NAME]).toBeDefined();
    }, 30000);

    it("should handle batch embeddings", async () => {
      const response = await client.embeddings.create({
        model: "togethercomputer/m2-bert-80M-8k-retrieval",
        input: ["Hello", "World"],
      });

      expect(response.data.length).toBe(2);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_TEXT}`]).toBe("Hello");
      expect(span.attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.1.${SemanticConventions.EMBEDDING_TEXT}`]).toBe("World");
    }, 30000);
  });

  describe("error handling", () => {
    it("should handle API errors gracefully", async () => {
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
});
