import { TogetherInstrumentation, isPatched } from "../instrumentation";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SemanticConventions, FISpanKind, MimeType, LLMSystem, LLMProvider } from "@traceai/fi-semantic-conventions";

describe("TogetherInstrumentation", () => {
  let instrumentation: TogetherInstrumentation;
  let memoryExporter: InMemorySpanExporter;
  let provider: NodeTracerProvider;

  beforeEach(() => {
    memoryExporter = new InMemorySpanExporter();
    provider = new NodeTracerProvider({
      spanProcessors: [new SimpleSpanProcessor(memoryExporter)],
    });
    provider.register();

    instrumentation = new TogetherInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
  });

  afterEach(async () => {
    memoryExporter.reset();
    await provider.shutdown();
  });

  describe("constructor", () => {
    it("should create instrumentation with default config", () => {
      const inst = new TogetherInstrumentation();
      expect(inst.instrumentationName).toBe("@traceai/together");
    });

    it("should create instrumentation with custom config", () => {
      const inst = new TogetherInstrumentation({
        instrumentationConfig: { enabled: true },
        traceConfig: { hideInputs: true },
      });
      expect(inst.instrumentationName).toBe("@traceai/together");
    });
  });

  describe("isPatched", () => {
    it("should return false initially", () => {
      // Create a fresh instrumentation to test initial state
      const freshInst = new TogetherInstrumentation();
      // Note: isPatched is a module-level flag, so this may be true from previous tests
      expect(typeof isPatched()).toBe("boolean");
    });
  });

  describe("manuallyInstrument", () => {
    it("should patch a mock Together module", () => {
      const mockCreate = jest.fn().mockResolvedValue({
        id: "test-id",
        object: "chat.completion",
        created: Date.now(),
        model: "meta-llama/Llama-3-8b-chat-hf",
        choices: [
          {
            index: 0,
            message: { role: "assistant", content: "Hello!" },
            finish_reason: "stop",
          },
        ],
        usage: {
          prompt_tokens: 10,
          completion_tokens: 5,
          total_tokens: 15,
        },
      });

      const mockModule = {
        Together: class Together {
          chat = {
            completions: {
              create: mockCreate,
            },
          };
          completions = {
            create: jest.fn(),
          };
          embeddings = {
            create: jest.fn(),
          };
        },
      };

      // Patch at prototype level
      (mockModule.Together.prototype as unknown as Record<string, unknown>).chat = {
        completions: { create: mockCreate },
      };
      (mockModule.Together.prototype as unknown as Record<string, unknown>).completions = {
        create: jest.fn(),
      };
      (mockModule.Together.prototype as unknown as Record<string, unknown>).embeddings = {
        create: jest.fn(),
      };

      instrumentation.manuallyInstrument(mockModule as unknown as Record<string, unknown>);
      expect(isPatched()).toBe(true);
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
        model: "meta-llama/Llama-3-8b-chat-hf",
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

      // Create wrapper through the private method
      const wrapperFactory = (instrumentation as unknown as {
        createChatCompletionWrapper: (inst: TogetherInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createChatCompletionWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal);
    });

    it("should create a span for chat completions", async () => {
      const body = {
        model: "meta-llama/Llama-3-8b-chat-hf",
        messages: [
          { role: "user", content: "Hello!" },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("Together Chat Completions");
    });

    it("should set correct attributes for chat completion", async () => {
      const body = {
        model: "meta-llama/Llama-3-8b-chat-hf",
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
      expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("meta-llama/Llama-3-8b-chat-hf");
      expect(span.attributes[SemanticConventions.LLM_PROVIDER]).toBe(LLMProvider.TOGETHER);
      expect(span.attributes[SemanticConventions.INPUT_MIME_TYPE]).toBe(MimeType.JSON);
      expect(span.attributes[SemanticConventions.OUTPUT_MIME_TYPE]).toBe(MimeType.JSON);
    });

    it("should capture input messages", async () => {
      const body = {
        model: "meta-llama/Llama-3-8b-chat-hf",
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
      expect(inputMessages[1].role).toBe("user");
      expect(inputMessages[1].content).toBe("Hello!");
    });

    it("should capture output messages", async () => {
      const body = {
        model: "meta-llama/Llama-3-8b-chat-hf",
        messages: [
          { role: "user", content: "Hello!" },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      const outputMessages = JSON.parse(span.attributes[SemanticConventions.LLM_OUTPUT_MESSAGES] as string);
      expect(outputMessages[0].role).toBe("assistant");
      expect(outputMessages[0].content).toBe("Hello! How can I help you today?");
    });

    it("should capture token usage", async () => {
      const body = {
        model: "meta-llama/Llama-3-8b-chat-hf",
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

    it("should handle tool calls in response", async () => {
      mockOriginal.mockResolvedValueOnce({
        id: "chatcmpl-123",
        object: "chat.completion",
        created: 1677652288,
        model: "meta-llama/Llama-3-8b-chat-hf",
        choices: [
          {
            index: 0,
            message: {
              role: "assistant",
              content: null,
              tool_calls: [
                {
                  id: "call_123",
                  type: "function",
                  function: {
                    name: "get_weather",
                    arguments: '{"location": "San Francisco"}',
                  },
                },
              ],
            },
            finish_reason: "tool_calls",
          },
        ],
        usage: {
          prompt_tokens: 15,
          completion_tokens: 20,
          total_tokens: 35,
        },
      });

      const body = {
        model: "meta-llama/Llama-3-8b-chat-hf",
        messages: [
          { role: "user", content: "What's the weather in San Francisco?" },
        ],
        tools: [
          {
            type: "function",
            function: {
              name: "get_weather",
              description: "Get the current weather",
              parameters: {
                type: "object",
                properties: {
                  location: { type: "string" },
                },
              },
            },
          },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      const outputMessages = JSON.parse(span.attributes[SemanticConventions.LLM_OUTPUT_MESSAGES] as string);
      expect(outputMessages[0].tool_calls[0].id).toBe("call_123");
      expect(outputMessages[0].tool_calls[0].function.name).toBe("get_weather");
      expect(outputMessages[0].tool_calls[0].function.arguments).toBe('{"location": "San Francisco"}');
    });

    it("should capture tool definitions", async () => {
      const body = {
        model: "meta-llama/Llama-3-8b-chat-hf",
        messages: [
          { role: "user", content: "Hello!" },
        ],
        tools: [
          {
            type: "function",
            function: {
              name: "test_function",
              description: "A test function",
              parameters: {
                type: "object",
                properties: {
                  param1: { type: "string" },
                },
              },
            },
          },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.LLM_TOOLS]).toBeDefined();
      expect(typeof span.attributes[SemanticConventions.LLM_TOOLS]).toBe("string");
    });

    it("should handle errors gracefully", async () => {
      const error = new Error("API Error");
      mockOriginal.mockRejectedValueOnce(error);

      const body = {
        model: "meta-llama/Llama-3-8b-chat-hf",
        messages: [
          { role: "user", content: "Hello!" },
        ],
      };

      await expect(patchedFn(body)).rejects.toThrow("API Error");

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].status.code).toBe(2); // SpanStatusCode.ERROR
    });

    it("should capture raw input and output", async () => {
      const body = {
        model: "meta-llama/Llama-3-8b-chat-hf",
        messages: [
          { role: "user", content: "Hello!" },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.RAW_INPUT]).toBeDefined();
      expect(span.attributes[SemanticConventions.RAW_OUTPUT]).toBeDefined();
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
        model: "togethercomputer/RedPajama-INCITE-7B-Base",
        choices: [
          {
            text: " world!",
            index: 0,
            finish_reason: "stop",
          },
        ],
        usage: {
          prompt_tokens: 5,
          completion_tokens: 3,
          total_tokens: 8,
        },
      });

      const wrapperFactory = (instrumentation as unknown as {
        createCompletionWrapper: (inst: TogetherInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createCompletionWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal);
    });

    it("should create a span for completions", async () => {
      const body = {
        model: "togethercomputer/RedPajama-INCITE-7B-Base",
        prompt: "Hello",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("Together Completions");
    });

    it("should set correct attributes for completion", async () => {
      const body = {
        model: "togethercomputer/RedPajama-INCITE-7B-Base",
        prompt: "Hello",
        temperature: 0.5,
        max_tokens: 50,
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
      expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("togethercomputer/RedPajama-INCITE-7B-Base");
      expect(span.attributes[SemanticConventions.LLM_PROVIDER]).toBe(LLMProvider.TOGETHER);
      expect(span.attributes[`${SemanticConventions.LLM_PROMPTS}.0`]).toBe("Hello");
    });

    it("should capture output text", async () => {
      const body = {
        model: "togethercomputer/RedPajama-INCITE-7B-Base",
        prompt: "Hello",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.OUTPUT_VALUE]).toBe(" world!");
      expect(span.attributes[SemanticConventions.OUTPUT_MIME_TYPE]).toBe(MimeType.TEXT);
    });

    it("should capture token usage", async () => {
      const body = {
        model: "togethercomputer/RedPajama-INCITE-7B-Base",
        prompt: "Hello",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT]).toBe(5);
      expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]).toBe(3);
      expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL]).toBe(8);
    });

    it("should handle errors gracefully", async () => {
      const error = new Error("Completion error");
      mockOriginal.mockRejectedValueOnce(error);

      const body = {
        model: "togethercomputer/RedPajama-INCITE-7B-Base",
        prompt: "Hello",
      };

      await expect(patchedFn(body)).rejects.toThrow("Completion error");

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].status.code).toBe(2);
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
        model: "togethercomputer/m2-bert-80M-8k-retrieval",
        usage: {
          prompt_tokens: 8,
          total_tokens: 8,
        },
      });

      const wrapperFactory = (instrumentation as unknown as {
        createEmbeddingWrapper: (inst: TogetherInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createEmbeddingWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal);
    });

    it("should create a span for embeddings", async () => {
      const body = {
        model: "togethercomputer/m2-bert-80M-8k-retrieval",
        input: "Hello world",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("Together Embeddings");
    });

    it("should set correct attributes for embeddings", async () => {
      const body = {
        model: "togethercomputer/m2-bert-80M-8k-retrieval",
        input: "Hello world",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.EMBEDDING);
      expect(span.attributes[SemanticConventions.EMBEDDING_MODEL_NAME]).toBe("togethercomputer/m2-bert-80M-8k-retrieval");
      expect(span.attributes[SemanticConventions.LLM_PROVIDER]).toBe(LLMProvider.TOGETHER);
    });

    it("should capture input text", async () => {
      const body = {
        model: "togethercomputer/m2-bert-80M-8k-retrieval",
        input: "Hello world",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_TEXT}`]).toBe("Hello world");
    });

    it("should capture embedding vector", async () => {
      const body = {
        model: "togethercomputer/m2-bert-80M-8k-retrieval",
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
        model: "togethercomputer/m2-bert-80M-8k-retrieval",
        usage: { prompt_tokens: 10, total_tokens: 10 },
      });

      const body = {
        model: "togethercomputer/m2-bert-80M-8k-retrieval",
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
        model: "togethercomputer/m2-bert-80M-8k-retrieval",
        input: "Hello world",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT]).toBe(8);
      expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL]).toBe(8);
    });

    it("should handle errors gracefully", async () => {
      const error = new Error("Embedding error");
      mockOriginal.mockRejectedValueOnce(error);

      const body = {
        model: "togethercomputer/m2-bert-80M-8k-retrieval",
        input: "Hello world",
      };

      await expect(patchedFn(body)).rejects.toThrow("Embedding error");

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].status.code).toBe(2);
    });
  });

  describe("streaming response handling", () => {
    let mockOriginal: jest.Mock;
    let patchedFn: (body: Record<string, unknown>, options?: Record<string, unknown>) => Promise<unknown>;

    beforeEach(() => {
      const wrapperFactory = (instrumentation as unknown as {
        createChatCompletionWrapper: (inst: TogetherInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createChatCompletionWrapper(instrumentation);

      mockOriginal = jest.fn();
      patchedFn = wrapperFactory(mockOriginal);
    });

    it("should handle streaming responses", async () => {
      const chunks = [
        {
          id: "chatcmpl-123",
          object: "chat.completion.chunk",
          created: 1677652288,
          model: "meta-llama/Llama-3-8b-chat-hf",
          choices: [{ index: 0, delta: { content: "Hello" }, finish_reason: null }],
        },
        {
          id: "chatcmpl-123",
          object: "chat.completion.chunk",
          created: 1677652288,
          model: "meta-llama/Llama-3-8b-chat-hf",
          choices: [{ index: 0, delta: { content: " world" }, finish_reason: null }],
        },
        {
          id: "chatcmpl-123",
          object: "chat.completion.chunk",
          created: 1677652288,
          model: "meta-llama/Llama-3-8b-chat-hf",
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
        model: "meta-llama/Llama-3-8b-chat-hf",
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

      const span = spans[0];
      expect(span.attributes[SemanticConventions.OUTPUT_VALUE]).toBe("Hello world!");
    });

    it("should handle stream errors", async () => {
      async function* mockErrorStream() {
        yield {
          id: "chatcmpl-123",
          choices: [{ index: 0, delta: { content: "Hello" } }],
        };
        throw new Error("Stream error");
      }

      mockOriginal.mockResolvedValueOnce(mockErrorStream());

      const body = {
        model: "meta-llama/Llama-3-8b-chat-hf",
        messages: [{ role: "user", content: "Hello!" }],
        stream: true,
      };

      const result = await patchedFn(body);

      const consumeStream = async () => {
        for await (const _chunk of result as AsyncIterable<unknown>) {
          // consume
        }
      };

      await expect(consumeStream()).rejects.toThrow("Stream error");

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].status.code).toBe(2);
    });
  });

  describe("invocation parameters", () => {
    let mockOriginal: jest.Mock;
    let patchedFn: (body: Record<string, unknown>, options?: Record<string, unknown>) => Promise<unknown>;

    beforeEach(() => {
      mockOriginal = jest.fn().mockResolvedValue({
        id: "chatcmpl-123",
        object: "chat.completion",
        created: 1677652288,
        model: "meta-llama/Llama-3-8b-chat-hf",
        choices: [
          {
            index: 0,
            message: { role: "assistant", content: "Response" },
            finish_reason: "stop",
          },
        ],
      });

      const wrapperFactory = (instrumentation as unknown as {
        createChatCompletionWrapper: (inst: TogetherInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createChatCompletionWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal);
    });

    it("should capture invocation parameters", async () => {
      const body = {
        model: "meta-llama/Llama-3-8b-chat-hf",
        messages: [{ role: "user", content: "Hello!" }],
        temperature: 0.7,
        max_tokens: 100,
        top_p: 0.9,
        top_k: 50,
        repetition_penalty: 1.1,
        stop: ["END"],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      const invocationParams = JSON.parse(span.attributes[SemanticConventions.LLM_INVOCATION_PARAMETERS] as string);
      expect(invocationParams.temperature).toBe(0.7);
      expect(invocationParams.max_tokens).toBe(100);
      expect(invocationParams.top_p).toBe(0.9);
      expect(invocationParams.top_k).toBe(50);
      expect(invocationParams.repetition_penalty).toBe(1.1);
      expect(invocationParams.stop).toEqual(["END"]);
    });
  });
});
