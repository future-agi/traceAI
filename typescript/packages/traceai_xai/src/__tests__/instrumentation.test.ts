import { XAIInstrumentation, isPatched } from "../instrumentation";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SemanticConventions, FISpanKind, MimeType, LLMSystem, LLMProvider } from "@traceai/fi-semantic-conventions";

describe("XAIInstrumentation", () => {
  let instrumentation: XAIInstrumentation;
  let memoryExporter: InMemorySpanExporter;
  let provider: NodeTracerProvider;

  beforeEach(() => {
    memoryExporter = new InMemorySpanExporter();
    provider = new NodeTracerProvider();
    provider.addSpanProcessor(new SimpleSpanProcessor(memoryExporter));
    provider.register();

    instrumentation = new XAIInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
  });

  afterEach(async () => {
    memoryExporter.reset();
    await provider.shutdown();
  });

  describe("constructor", () => {
    it("should create instrumentation with default config", () => {
      const inst = new XAIInstrumentation();
      expect(inst.instrumentationName).toBe("@traceai/xai");
    });

    it("should create instrumentation with custom config", () => {
      const inst = new XAIInstrumentation({
        instrumentationConfig: { enabled: true },
        traceConfig: { hideInputs: true },
      });
      expect(inst.instrumentationName).toBe("@traceai/xai");
    });
  });

  describe("isPatched", () => {
    it("should return a boolean", () => {
      expect(typeof isPatched()).toBe("boolean");
    });
  });

  describe("isXAIRequest", () => {
    it("should identify xAI API requests", () => {
      const isXAIRequest = (instrumentation as unknown as { isXAIRequest: (client: unknown) => boolean }).isXAIRequest;

      expect(isXAIRequest.call(instrumentation, { baseURL: "https://api.x.ai/v1" })).toBe(true);
      expect(isXAIRequest.call(instrumentation, { baseURL: "https://x.ai/api/v1" })).toBe(true);
      expect(isXAIRequest.call(instrumentation, { baseURL: "https://api.openai.com/v1" })).toBe(false);
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
        model: "grok-beta",
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
        createChatCompletionWrapper: (inst: XAIInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createChatCompletionWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal).bind({ baseURL: "https://api.x.ai/v1" });
    });

    it("should create a span for chat completions", async () => {
      const body = {
        model: "grok-beta",
        messages: [
          { role: "user", content: "Hello!" },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("xAI Chat Completions");
    });

    it("should set correct attributes for chat completion", async () => {
      const body = {
        model: "grok-beta",
        messages: [
          { role: "system", content: "You are Grok, a witty AI assistant." },
          { role: "user", content: "Hello!" },
        ],
        temperature: 0.7,
        max_tokens: 100,
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
      expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("grok-beta");
      expect(span.attributes[SemanticConventions.LLM_SYSTEM]).toBe(LLMSystem.XAI);
      expect(span.attributes[SemanticConventions.LLM_PROVIDER]).toBe(LLMProvider.XAI);
      expect(span.attributes[SemanticConventions.INPUT_MIME_TYPE]).toBe(MimeType.JSON);
    });

    it("should capture input messages", async () => {
      const body = {
        model: "grok-beta",
        messages: [
          { role: "system", content: "You are Grok." },
          { role: "user", content: "Hello!" },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[`${SemanticConventions.LLM_INPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_ROLE}`]).toBe("system");
      expect(span.attributes[`${SemanticConventions.LLM_INPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_CONTENT}`]).toBe("You are Grok.");
      expect(span.attributes[`${SemanticConventions.LLM_INPUT_MESSAGES}.1.${SemanticConventions.MESSAGE_ROLE}`]).toBe("user");
    });

    it("should capture output messages", async () => {
      const body = {
        model: "grok-beta",
        messages: [
          { role: "user", content: "Hello!" },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[`${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_ROLE}`]).toBe("assistant");
      expect(span.attributes[`${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_CONTENT}`]).toBe("Hello! How can I help you today?");
    });

    it("should capture token usage", async () => {
      const body = {
        model: "grok-beta",
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
        model: "grok-beta",
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
                    name: "get_current_time",
                    arguments: '{"timezone": "Asia/Tokyo"}',
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
        model: "grok-beta",
        messages: [
          { role: "user", content: "What's the time in Tokyo?" },
        ],
        tools: [
          {
            type: "function",
            function: {
              name: "get_current_time",
              description: "Get the current time in a timezone",
              parameters: {
                type: "object",
                properties: {
                  timezone: { type: "string" },
                },
              },
            },
          },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      const toolCallPrefix = `${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_TOOL_CALLS}.0.`;
      expect(span.attributes[`${toolCallPrefix}${SemanticConventions.TOOL_CALL_ID}`]).toBe("call_123");
      expect(span.attributes[`${toolCallPrefix}${SemanticConventions.TOOL_CALL_FUNCTION_NAME}`]).toBe("get_current_time");
      expect(span.attributes[`${toolCallPrefix}${SemanticConventions.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}`]).toBe('{"timezone": "Asia/Tokyo"}');
    });

    it("should capture tool definitions", async () => {
      const body = {
        model: "grok-beta",
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

      const toolKey = `${SemanticConventions.LLM_TOOLS}.0.${SemanticConventions.TOOL_JSON_SCHEMA}`;
      expect(span.attributes[toolKey]).toBeDefined();
      expect(typeof span.attributes[toolKey]).toBe("string");
    });

    it("should handle errors gracefully", async () => {
      const error = new Error("API Error");
      mockOriginal.mockRejectedValueOnce(error);

      const body = {
        model: "grok-beta",
        messages: [
          { role: "user", content: "Hello!" },
        ],
      };

      await expect(patchedFn(body)).rejects.toThrow("API Error");

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].status.code).toBe(2);
    });

    it("should capture raw input and output", async () => {
      const body = {
        model: "grok-beta",
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
        model: "grok-embed",
        usage: {
          prompt_tokens: 8,
          total_tokens: 8,
        },
      });

      const wrapperFactory = (instrumentation as unknown as {
        createEmbeddingWrapper: (inst: XAIInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createEmbeddingWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal).bind({ baseURL: "https://api.x.ai/v1" });
    });

    it("should create a span for embeddings", async () => {
      const body = {
        model: "grok-embed",
        input: "Hello world",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("xAI Embeddings");
    });

    it("should set correct attributes for embeddings", async () => {
      const body = {
        model: "grok-embed",
        input: "Hello world",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.EMBEDDING);
      expect(span.attributes[SemanticConventions.EMBEDDING_MODEL_NAME]).toBe("grok-embed");
      expect(span.attributes[SemanticConventions.LLM_SYSTEM]).toBe(LLMSystem.XAI);
      expect(span.attributes[SemanticConventions.LLM_PROVIDER]).toBe(LLMProvider.XAI);
    });

    it("should capture input text", async () => {
      const body = {
        model: "grok-embed",
        input: "Hello world",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_TEXT}`]).toBe("Hello world");
    });

    it("should capture embedding vector", async () => {
      const body = {
        model: "grok-embed",
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
        model: "grok-embed",
        usage: { prompt_tokens: 10, total_tokens: 10 },
      });

      const body = {
        model: "grok-embed",
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
        model: "grok-embed",
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
        model: "grok-embed",
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
        createChatCompletionWrapper: (inst: XAIInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createChatCompletionWrapper(instrumentation);

      mockOriginal = jest.fn();
      patchedFn = wrapperFactory(mockOriginal).bind({ baseURL: "https://api.x.ai/v1" });
    });

    it("should handle streaming responses", async () => {
      const chunks = [
        {
          id: "chatcmpl-123",
          object: "chat.completion.chunk",
          created: 1677652288,
          model: "grok-beta",
          choices: [{ index: 0, delta: { content: "Hello" }, finish_reason: null }],
        },
        {
          id: "chatcmpl-123",
          object: "chat.completion.chunk",
          created: 1677652288,
          model: "grok-beta",
          choices: [{ index: 0, delta: { content: " world" }, finish_reason: null }],
        },
        {
          id: "chatcmpl-123",
          object: "chat.completion.chunk",
          created: 1677652288,
          model: "grok-beta",
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
        model: "grok-beta",
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
        model: "grok-beta",
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
        model: "grok-beta",
        choices: [
          {
            index: 0,
            message: { role: "assistant", content: "Response" },
            finish_reason: "stop",
          },
        ],
      });

      const wrapperFactory = (instrumentation as unknown as {
        createChatCompletionWrapper: (inst: XAIInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createChatCompletionWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal).bind({ baseURL: "https://api.x.ai/v1" });
    });

    it("should capture invocation parameters", async () => {
      const body = {
        model: "grok-beta",
        messages: [{ role: "user", content: "Hello!" }],
        temperature: 0.7,
        max_tokens: 100,
        top_p: 0.9,
        stop: ["END"],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      const invocationParams = JSON.parse(span.attributes[SemanticConventions.LLM_INVOCATION_PARAMETERS] as string);
      expect(invocationParams.temperature).toBe(0.7);
      expect(invocationParams.max_tokens).toBe(100);
      expect(invocationParams.top_p).toBe(0.9);
      expect(invocationParams.stop).toEqual(["END"]);
    });
  });
});
