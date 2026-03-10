import { FireworksInstrumentation, isPatched } from "../instrumentation";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SemanticConventions, FISpanKind, MimeType, LLMSystem, LLMProvider } from "@traceai/fi-semantic-conventions";

describe("FireworksInstrumentation", () => {
  let instrumentation: FireworksInstrumentation;
  let memoryExporter: InMemorySpanExporter;
  let provider: NodeTracerProvider;

  beforeEach(() => {
    memoryExporter = new InMemorySpanExporter();
    provider = new NodeTracerProvider({
      spanProcessors: [new SimpleSpanProcessor(memoryExporter)],
    });
    provider.register();

    instrumentation = new FireworksInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
  });

  afterEach(async () => {
    memoryExporter.reset();
    await provider.shutdown();
  });

  describe("constructor", () => {
    it("should create instrumentation with default config", () => {
      const inst = new FireworksInstrumentation();
      expect(inst.instrumentationName).toBe("@traceai/fireworks");
    });

    it("should create instrumentation with custom config", () => {
      const inst = new FireworksInstrumentation({
        instrumentationConfig: { enabled: true },
        traceConfig: { hideInputs: true },
      });
      expect(inst.instrumentationName).toBe("@traceai/fireworks");
    });
  });

  describe("isPatched", () => {
    it("should return a boolean", () => {
      expect(typeof isPatched()).toBe("boolean");
    });
  });

  describe("isFireworksRequest", () => {
    it("should identify Fireworks API requests", () => {
      const isFireworksRequest = (instrumentation as unknown as { isFireworksRequest: (client: unknown) => boolean }).isFireworksRequest;

      expect(isFireworksRequest.call(instrumentation, { baseURL: "https://api.fireworks.ai/inference/v1" })).toBe(true);
      expect(isFireworksRequest.call(instrumentation, { baseURL: "https://fireworks.example.com/v1" })).toBe(true);
      expect(isFireworksRequest.call(instrumentation, { baseURL: "https://api.openai.com/v1" })).toBe(false);
    });
  });

  describe("chat.completions.create wrapper", () => {
    let mockOriginal: jest.Mock;
    let patchedFn: (body: Record<string, unknown>, options?: Record<string, unknown>) => Promise<unknown>;

    beforeEach(() => {
      mockOriginal = jest.fn().mockResolvedValue({
        id: "chatcmpl-123",
        object: "chat.completion",
        created: 1677652288,
        model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
        choices: [
          {
            index: 0,
            message: {
              role: "assistant",
              content: "Hello! How can I help you today?",
            },
            finish_reason: "stop",
          },
        ],
        usage: {
          prompt_tokens: 10,
          completion_tokens: 12,
          total_tokens: 22,
        },
      });

      const wrapperFactory = (instrumentation as unknown as {
        createChatCompletionWrapper: (inst: FireworksInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createChatCompletionWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal).bind({ baseURL: "https://api.fireworks.ai/inference/v1" });
    });

    it("should create a span for chat completions", async () => {
      const body = {
        model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
        messages: [
          { role: "user", content: "Hello!" },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("Fireworks Chat Completions");
    });

    it("should set correct attributes for chat completion", async () => {
      const body = {
        model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
        messages: [
          { role: "system", content: "You are a helpful assistant." },
          { role: "user", content: "Hello!" },
        ],
        temperature: 0.7,
        max_tokens: 100,
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
      expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("accounts/fireworks/models/llama-v3p1-8b-instruct");
      expect(span.attributes[SemanticConventions.LLM_PROVIDER]).toBe(LLMProvider.FIREWORKS);
      expect(span.attributes[SemanticConventions.INPUT_MIME_TYPE]).toBe(MimeType.JSON);
    });

    it("should capture input messages", async () => {
      const body = {
        model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
        messages: [
          { role: "system", content: "You are a helpful assistant." },
          { role: "user", content: "Hello!" },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      const inputMessages = JSON.parse(span.attributes[SemanticConventions.LLM_INPUT_MESSAGES] as string);
      expect(inputMessages[0].role).toBe("system");
      expect(inputMessages[0].content).toBe("You are a helpful assistant.");
    });

    it("should capture token usage", async () => {
      const body = {
        model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
        messages: [
          { role: "user", content: "Hello!" },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT]).toBe(10);
      expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]).toBe(12);
      expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL]).toBe(22);
    });

    it("should handle errors gracefully", async () => {
      const error = new Error("API Error");
      mockOriginal.mockRejectedValueOnce(error);

      const body = {
        model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
        messages: [
          { role: "user", content: "Hello!" },
        ],
      };

      await expect(patchedFn(body)).rejects.toThrow("API Error");

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].status.code).toBe(2);
    });
  });

  describe("completions.create wrapper", () => {
    let mockOriginal: jest.Mock;
    let patchedFn: (body: Record<string, unknown>, options?: Record<string, unknown>) => Promise<unknown>;

    beforeEach(() => {
      mockOriginal = jest.fn().mockResolvedValue({
        id: "cmpl-123",
        object: "text_completion",
        created: 1677652288,
        model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
        choices: [
          {
            text: " jumped over the lazy dog.",
            index: 0,
            finish_reason: "stop",
          },
        ],
        usage: {
          prompt_tokens: 5,
          completion_tokens: 8,
          total_tokens: 13,
        },
      });

      const wrapperFactory = (instrumentation as unknown as {
        createCompletionWrapper: (inst: FireworksInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createCompletionWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal).bind({ baseURL: "https://api.fireworks.ai/inference/v1" });
    });

    it("should create a span for completions", async () => {
      const body = {
        model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
        prompt: "The quick brown fox",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("Fireworks Completions");
    });

    it("should capture output text", async () => {
      const body = {
        model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
        prompt: "The quick brown fox",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.OUTPUT_VALUE]).toBe(" jumped over the lazy dog.");
      expect(span.attributes[SemanticConventions.OUTPUT_MIME_TYPE]).toBe(MimeType.TEXT);
    });
  });

  describe("embeddings.create wrapper", () => {
    let mockOriginal: jest.Mock;
    let patchedFn: (body: Record<string, unknown>, options?: Record<string, unknown>) => Promise<unknown>;

    beforeEach(() => {
      mockOriginal = jest.fn().mockResolvedValue({
        object: "list",
        data: [
          {
            object: "embedding",
            embedding: [0.1, 0.2, 0.3, 0.4, 0.5],
            index: 0,
          },
        ],
        model: "nomic-ai/nomic-embed-text-v1.5",
        usage: {
          prompt_tokens: 8,
          total_tokens: 8,
        },
      });

      const wrapperFactory = (instrumentation as unknown as {
        createEmbeddingWrapper: (inst: FireworksInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createEmbeddingWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal).bind({ baseURL: "https://api.fireworks.ai/inference/v1" });
    });

    it("should create a span for embeddings", async () => {
      const body = {
        model: "nomic-ai/nomic-embed-text-v1.5",
        input: "Hello world",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("Fireworks Embeddings");
    });

    it("should set correct attributes for embeddings", async () => {
      const body = {
        model: "nomic-ai/nomic-embed-text-v1.5",
        input: "Hello world",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.EMBEDDING);
      expect(span.attributes[SemanticConventions.EMBEDDING_MODEL_NAME]).toBe("nomic-ai/nomic-embed-text-v1.5");
      expect(span.attributes[SemanticConventions.LLM_PROVIDER]).toBe(LLMProvider.FIREWORKS);
    });

    it("should capture input text", async () => {
      const body = {
        model: "nomic-ai/nomic-embed-text-v1.5",
        input: "Hello world",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_TEXT}`]).toBe("Hello world");
    });

    it("should capture embedding vector", async () => {
      const body = {
        model: "nomic-ai/nomic-embed-text-v1.5",
        input: "Hello world",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      const vectorAttr = span.attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_VECTOR}`];
      expect(vectorAttr).toBeDefined();
      expect(JSON.parse(vectorAttr as string)).toEqual([0.1, 0.2, 0.3, 0.4, 0.5]);
    });

    it("should handle batch embeddings", async () => {
      mockOriginal.mockResolvedValueOnce({
        object: "list",
        data: [
          { object: "embedding", embedding: [0.1, 0.2], index: 0 },
          { object: "embedding", embedding: [0.3, 0.4], index: 1 },
        ],
        model: "nomic-ai/nomic-embed-text-v1.5",
        usage: { prompt_tokens: 10, total_tokens: 10 },
      });

      const body = {
        model: "nomic-ai/nomic-embed-text-v1.5",
        input: ["Hello", "World"],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_TEXT}`]).toBe("Hello");
      expect(span.attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.1.${SemanticConventions.EMBEDDING_TEXT}`]).toBe("World");
    });

    it("should capture token usage", async () => {
      const body = {
        model: "nomic-ai/nomic-embed-text-v1.5",
        input: "Hello world",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT]).toBe(8);
      expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL]).toBe(8);
    });
  });

  describe("streaming response handling", () => {
    let mockOriginal: jest.Mock;
    let patchedFn: (body: Record<string, unknown>, options?: Record<string, unknown>) => Promise<unknown>;

    beforeEach(() => {
      const wrapperFactory = (instrumentation as unknown as {
        createChatCompletionWrapper: (inst: FireworksInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createChatCompletionWrapper(instrumentation);

      mockOriginal = jest.fn();
      patchedFn = wrapperFactory(mockOriginal).bind({ baseURL: "https://api.fireworks.ai/inference/v1" });
    });

    it("should handle streaming responses", async () => {
      const chunks = [
        {
          id: "chatcmpl-123",
          object: "chat.completion.chunk",
          created: 1677652288,
          model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
          choices: [{ index: 0, delta: { content: "Hello" }, finish_reason: null }],
        },
        {
          id: "chatcmpl-123",
          object: "chat.completion.chunk",
          created: 1677652288,
          model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
          choices: [{ index: 0, delta: { content: " world" }, finish_reason: null }],
        },
        {
          id: "chatcmpl-123",
          object: "chat.completion.chunk",
          created: 1677652288,
          model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
          choices: [{ index: 0, delta: { content: "!" }, finish_reason: "stop" }],
          usage: { prompt_tokens: 5, completion_tokens: 3, total_tokens: 8 },
        },
      ];

      async function* mockStream() {
        for (const chunk of chunks) {
          yield chunk;
        }
      }

      mockOriginal.mockResolvedValueOnce(mockStream());

      const body = {
        model: "accounts/fireworks/models/llama-v3p1-8b-instruct",
        messages: [{ role: "user", content: "Hello!" }],
        stream: true,
      };

      const result = await patchedFn(body);
      const collectedChunks: unknown[] = [];

      for await (const chunk of result as AsyncIterable<unknown>) {
        collectedChunks.push(chunk);
      }

      expect(collectedChunks.length).toBe(3);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].attributes[SemanticConventions.OUTPUT_VALUE]).toBe("Hello world!");
    });
  });
});
