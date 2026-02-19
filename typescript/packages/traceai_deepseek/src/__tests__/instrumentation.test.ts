import { DeepSeekInstrumentation, isPatched } from "../instrumentation";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SemanticConventions, FISpanKind, MimeType, LLMSystem, LLMProvider } from "@traceai/fi-semantic-conventions";

describe("DeepSeekInstrumentation", () => {
  let instrumentation: DeepSeekInstrumentation;
  let memoryExporter: InMemorySpanExporter;
  let provider: NodeTracerProvider;

  beforeEach(() => {
    memoryExporter = new InMemorySpanExporter();
    provider = new NodeTracerProvider();
    provider.addSpanProcessor(new SimpleSpanProcessor(memoryExporter));
    provider.register();

    instrumentation = new DeepSeekInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
  });

  afterEach(async () => {
    memoryExporter.reset();
    await provider.shutdown();
  });

  describe("constructor", () => {
    it("should create instrumentation with default config", () => {
      const inst = new DeepSeekInstrumentation();
      expect(inst.instrumentationName).toBe("@traceai/deepseek");
    });

    it("should create instrumentation with custom config", () => {
      const inst = new DeepSeekInstrumentation({
        instrumentationConfig: { enabled: true },
        traceConfig: { hideInputs: true },
      });
      expect(inst.instrumentationName).toBe("@traceai/deepseek");
    });
  });

  describe("isPatched", () => {
    it("should return a boolean", () => {
      expect(typeof isPatched()).toBe("boolean");
    });
  });

  describe("isDeepSeekRequest", () => {
    it("should identify DeepSeek API requests", () => {
      const isDeepSeekRequest = (instrumentation as unknown as { isDeepSeekRequest: (client: unknown) => boolean }).isDeepSeekRequest;

      expect(isDeepSeekRequest.call(instrumentation, { baseURL: "https://api.deepseek.com/v1" })).toBe(true);
      expect(isDeepSeekRequest.call(instrumentation, { baseURL: "https://deepseek.example.com/v1" })).toBe(true);
      expect(isDeepSeekRequest.call(instrumentation, { baseURL: "https://api.openai.com/v1" })).toBe(false);
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
        model: "deepseek-chat",
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
        createChatCompletionWrapper: (inst: DeepSeekInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createChatCompletionWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal).bind({ baseURL: "https://api.deepseek.com/v1" });
    });

    it("should create a span for chat completions", async () => {
      const body = {
        model: "deepseek-chat",
        messages: [
          { role: "user", content: "Hello!" },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("DeepSeek Chat Completions");
    });

    it("should set correct attributes for chat completion", async () => {
      const body = {
        model: "deepseek-chat",
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
      expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("deepseek-chat");
      expect(span.attributes[SemanticConventions.LLM_SYSTEM]).toBe(LLMSystem.DEEPSEEK);
      expect(span.attributes[SemanticConventions.LLM_PROVIDER]).toBe(LLMProvider.DEEPSEEK);
      expect(span.attributes[SemanticConventions.INPUT_MIME_TYPE]).toBe(MimeType.JSON);
    });

    it("should capture input messages", async () => {
      const body = {
        model: "deepseek-chat",
        messages: [
          { role: "system", content: "You are a helpful assistant." },
          { role: "user", content: "Hello!" },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[`${SemanticConventions.LLM_INPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_ROLE}`]).toBe("system");
      expect(span.attributes[`${SemanticConventions.LLM_INPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_CONTENT}`]).toBe("You are a helpful assistant.");
    });

    it("should capture output messages", async () => {
      const body = {
        model: "deepseek-chat",
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
        model: "deepseek-chat",
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

    it("should capture DeepSeek R1 reasoning content", async () => {
      mockOriginal.mockResolvedValueOnce({
        id: "chatcmpl-123",
        object: "chat.completion",
        created: 1677652288,
        model: "deepseek-reasoner",
        choices: [
          {
            index: 0,
            message: {
              role: "assistant",
              content: "The answer is 405.",
              reasoning_content: "15 * 27 = 15 * 20 + 15 * 7 = 300 + 105 = 405",
            },
            finish_reason: "stop",
          },
        ],
        usage: {
          prompt_tokens: 20,
          completion_tokens: 50,
          total_tokens: 70,
        },
      });

      const body = {
        model: "deepseek-reasoner",
        messages: [
          { role: "user", content: "What is 15 * 27?" },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes["deepseek.reasoning_content"]).toBe("15 * 27 = 15 * 20 + 15 * 7 = 300 + 105 = 405");
    });

    it("should capture DeepSeek cache metrics", async () => {
      mockOriginal.mockResolvedValueOnce({
        id: "chatcmpl-123",
        object: "chat.completion",
        created: 1677652288,
        model: "deepseek-chat",
        choices: [
          {
            index: 0,
            message: {
              role: "assistant",
              content: "Hello!",
            },
            finish_reason: "stop",
          },
        ],
        usage: {
          prompt_tokens: 100,
          completion_tokens: 10,
          total_tokens: 110,
          prompt_cache_hit_tokens: 80,
          prompt_cache_miss_tokens: 20,
        },
      });

      const body = {
        model: "deepseek-chat",
        messages: [
          { role: "user", content: "Hello!" },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes["deepseek.prompt_cache_hit_tokens"]).toBe(80);
      expect(span.attributes["deepseek.prompt_cache_miss_tokens"]).toBe(20);
    });

    it("should handle tool calls in response", async () => {
      mockOriginal.mockResolvedValueOnce({
        id: "chatcmpl-123",
        object: "chat.completion",
        created: 1677652288,
        model: "deepseek-chat",
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
                    arguments: '{"location": "Paris"}',
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
        model: "deepseek-chat",
        messages: [
          { role: "user", content: "What's the weather in Paris?" },
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

      const toolCallPrefix = `${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_TOOL_CALLS}.0.`;
      expect(span.attributes[`${toolCallPrefix}${SemanticConventions.TOOL_CALL_ID}`]).toBe("call_123");
      expect(span.attributes[`${toolCallPrefix}${SemanticConventions.TOOL_CALL_FUNCTION_NAME}`]).toBe("get_weather");
    });

    it("should handle errors gracefully", async () => {
      const error = new Error("API Error");
      mockOriginal.mockRejectedValueOnce(error);

      const body = {
        model: "deepseek-chat",
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

  describe("streaming response handling", () => {
    let mockOriginal: jest.Mock;
    let patchedFn: (body: Record<string, unknown>, options?: Record<string, unknown>) => Promise<unknown>;

    beforeEach(() => {
      const wrapperFactory = (instrumentation as unknown as {
        createChatCompletionWrapper: (inst: DeepSeekInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createChatCompletionWrapper(instrumentation);

      mockOriginal = jest.fn();
      patchedFn = wrapperFactory(mockOriginal).bind({ baseURL: "https://api.deepseek.com/v1" });
    });

    it("should handle streaming responses", async () => {
      const chunks = [
        {
          id: "chatcmpl-123",
          object: "chat.completion.chunk",
          created: 1677652288,
          model: "deepseek-chat",
          choices: [{ index: 0, delta: { content: "Hello" }, finish_reason: null }],
        },
        {
          id: "chatcmpl-123",
          object: "chat.completion.chunk",
          created: 1677652288,
          model: "deepseek-chat",
          choices: [{ index: 0, delta: { content: " world" }, finish_reason: null }],
        },
        {
          id: "chatcmpl-123",
          object: "chat.completion.chunk",
          created: 1677652288,
          model: "deepseek-chat",
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
        model: "deepseek-chat",
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

    it("should capture reasoning content in streaming", async () => {
      const chunks = [
        {
          id: "chatcmpl-123",
          choices: [{ index: 0, delta: { reasoning_content: "Let me think" }, finish_reason: null }],
        },
        {
          id: "chatcmpl-123",
          choices: [{ index: 0, delta: { reasoning_content: "... 2+2=4" }, finish_reason: null }],
        },
        {
          id: "chatcmpl-123",
          choices: [{ index: 0, delta: { content: "4" }, finish_reason: "stop" }],
        },
      ];

      async function* mockStream() {
        for (const chunk of chunks) {
          yield chunk;
        }
      }

      mockOriginal.mockResolvedValueOnce(mockStream());

      const body = {
        model: "deepseek-reasoner",
        messages: [{ role: "user", content: "What is 2+2?" }],
        stream: true,
      };

      const result = await patchedFn(body);
      for await (const _chunk of result as AsyncIterable<unknown>) {
        // consume
      }

      const spans = memoryExporter.getFinishedSpans();
      expect(spans[0].attributes["deepseek.reasoning_content"]).toBe("Let me think... 2+2=4");
    });
  });
});
