/**
 * End-to-end tests for HuggingFace instrumentation.
 *
 * These tests require a valid HF_TOKEN environment variable.
 * Skip these tests in CI environments without API access.
 */
import { HuggingFaceInstrumentation } from "../instrumentation";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SemanticConventions, FISpanKind, LLMProvider } from "@traceai/fi-semantic-conventions";

// Check if we have API access
const hasApiKey = !!process.env.HF_TOKEN;

// Use conditional describe
const describeE2E = hasApiKey ? describe : describe.skip;

describeE2E("HuggingFaceInstrumentation E2E Tests", () => {
  let instrumentation: HuggingFaceInstrumentation;
  let memoryExporter: InMemorySpanExporter;
  let provider: NodeTracerProvider;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let HfInference: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let client: any;

  beforeAll(async () => {
    // Set up OpenTelemetry
    memoryExporter = new InMemorySpanExporter();
    provider = new NodeTracerProvider();
    provider.addSpanProcessor(new SimpleSpanProcessor(memoryExporter));
    provider.register();

    // Create and enable instrumentation
    instrumentation = new HuggingFaceInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    // Dynamically import @huggingface/inference
    const hfModule = await import("@huggingface/inference");
    HfInference = hfModule.HfInference;

    // Manually patch the module
    instrumentation.manuallyInstrument(hfModule);

    // Create client
    client = new HfInference(process.env.HF_TOKEN);
  });

  afterAll(async () => {
    await provider.shutdown();
  });

  beforeEach(() => {
    memoryExporter.reset();
  });

  describe("textGeneration", () => {
    it("should instrument text generation", async () => {
      const response = await client.textGeneration({
        model: "gpt2",
        inputs: "The quick brown fox",
        parameters: {
          max_new_tokens: 20,
        },
      });

      expect(response).toBeDefined();
      expect(response.generated_text).toBeDefined();

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);

      const span = spans[0];
      expect(span.name).toBe("HuggingFace Text Generation");
      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
      expect(span.attributes[SemanticConventions.LLM_PROVIDER]).toBe(LLMProvider.HUGGINGFACE);
    }, 60000);

    it("should capture input prompt", async () => {
      const inputText = "Once upon a time";
      await client.textGeneration({
        model: "gpt2",
        inputs: inputText,
        parameters: { max_new_tokens: 10 },
      });

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.INPUT_VALUE]).toBe(inputText);
      expect(span.attributes[`${SemanticConventions.LLM_PROMPTS}.0`]).toBe(inputText);
    }, 60000);
  });

  describe("chatCompletion", () => {
    it("should instrument chat completion", async () => {
      const response = await client.chatCompletion({
        model: "meta-llama/Llama-2-7b-chat-hf",
        messages: [
          { role: "user", content: "Say hello." },
        ],
        max_tokens: 10,
      });

      expect(response).toBeDefined();
      expect(response.choices).toBeDefined();
      expect(response.choices.length).toBeGreaterThan(0);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);

      const span = spans[0];
      expect(span.name).toBe("HuggingFace Chat Completion");
      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
    }, 60000);

    it("should capture input/output messages", async () => {
      await client.chatCompletion({
        model: "meta-llama/Llama-2-7b-chat-hf",
        messages: [
          { role: "system", content: "You are helpful." },
          { role: "user", content: "Hi!" },
        ],
        max_tokens: 10,
      });

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[`${SemanticConventions.LLM_INPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_ROLE}`]).toBe("system");
      expect(span.attributes[`${SemanticConventions.LLM_INPUT_MESSAGES}.1.${SemanticConventions.MESSAGE_ROLE}`]).toBe("user");
      expect(span.attributes[`${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_ROLE}`]).toBe("assistant");
    }, 60000);
  });

  describe("chatCompletionStream", () => {
    it("should instrument streaming chat completion", async () => {
      const stream = await client.chatCompletionStream({
        model: "meta-llama/Llama-2-7b-chat-hf",
        messages: [
          { role: "user", content: "Say hi." },
        ],
        max_tokens: 5,
      });

      const chunks: unknown[] = [];
      for await (const chunk of stream) {
        chunks.push(chunk);
      }

      expect(chunks.length).toBeGreaterThan(0);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);

      const span = spans[0];
      expect(span.name).toBe("HuggingFace Chat Completion Stream");
      expect(span.attributes[SemanticConventions.OUTPUT_VALUE]).toBeDefined();
    }, 60000);
  });

  describe("featureExtraction", () => {
    it("should instrument feature extraction (embeddings)", async () => {
      const response = await client.featureExtraction({
        model: "sentence-transformers/all-MiniLM-L6-v2",
        inputs: "Hello world",
      });

      expect(response).toBeDefined();
      expect(Array.isArray(response)).toBe(true);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);

      const span = spans[0];
      expect(span.name).toBe("HuggingFace Feature Extraction");
      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.EMBEDDING);
    }, 60000);

    it("should capture embedding input", async () => {
      const inputText = "Test embedding";
      await client.featureExtraction({
        model: "sentence-transformers/all-MiniLM-L6-v2",
        inputs: inputText,
      });

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_TEXT}`]).toBe(inputText);
    }, 60000);
  });

  describe("summarization", () => {
    it("should instrument summarization", async () => {
      const longText = `The tower is 324 metres (1,063 ft) tall, about the same height as an 81-storey building,
      and the tallest structure in Paris. Its base is square, measuring 125 metres (410 ft) on each side.
      During its construction, the Eiffel Tower surpassed the Washington Monument to become the tallest
      man-made structure in the world, a title it held for 41 years until the Chrysler Building in New York City
      was finished in 1930.`;

      const response = await client.summarization({
        model: "facebook/bart-large-cnn",
        inputs: longText,
        parameters: {
          max_length: 50,
        },
      });

      expect(response).toBeDefined();
      expect(response.summary_text).toBeDefined();

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);

      const span = spans[0];
      expect(span.name).toBe("HuggingFace Summarization");
      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
    }, 60000);
  });

  describe("translation", () => {
    it("should instrument translation", async () => {
      const response = await client.translation({
        model: "Helsinki-NLP/opus-mt-en-fr",
        inputs: "Hello, how are you?",
      });

      expect(response).toBeDefined();
      expect(response.translation_text).toBeDefined();

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);

      const span = spans[0];
      expect(span.name).toBe("HuggingFace Translation");
      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
    }, 60000);
  });

  describe("questionAnswering", () => {
    it("should instrument question answering", async () => {
      const response = await client.questionAnswering({
        model: "deepset/roberta-base-squad2",
        inputs: {
          question: "What is the capital of France?",
          context: "Paris is the capital and most populous city of France. It has been one of Europe's major centres of finance, diplomacy, commerce, fashion, science and arts.",
        },
      });

      expect(response).toBeDefined();
      expect(response.answer).toBeDefined();

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);

      const span = spans[0];
      expect(span.name).toBe("HuggingFace Question Answering");
      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
      expect(span.attributes[SemanticConventions.OUTPUT_VALUE]).toBeDefined();
    }, 60000);
  });

  describe("error handling", () => {
    it("should handle API errors gracefully", async () => {
      await expect(
        client.textGeneration({
          model: "non-existent-model-xyz123",
          inputs: "Hello",
        })
      ).rejects.toThrow();

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].status.code).toBe(2); // ERROR
    }, 60000);
  });
});
