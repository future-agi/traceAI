import { VLLMInstrumentation, isPatched } from "../instrumentation";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SemanticConventions, FISpanKind, MimeType, LLMSystem, LLMProvider } from "@traceai/fi-semantic-conventions";

describe("VLLMInstrumentation", () => {
  let instrumentation: VLLMInstrumentation;
  let memoryExporter: InMemorySpanExporter;
  let provider: NodeTracerProvider;

  beforeEach(() => {
    memoryExporter = new InMemorySpanExporter();
    provider = new NodeTracerProvider({
      spanProcessors: [new SimpleSpanProcessor(memoryExporter)],
    });
    provider.register();

    instrumentation = new VLLMInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
  });

  afterEach(async () => {
    memoryExporter.reset();
    await provider.shutdown();
  });

  describe("constructor", () => {
    it("should create instrumentation with default config", () => {
      const inst = new VLLMInstrumentation();
      expect(inst.instrumentationName).toBe("@traceai/vllm");
    });

    it("should create instrumentation with custom config", () => {
      const inst = new VLLMInstrumentation({
        instrumentationConfig: { enabled: true },
        traceConfig: { hideInputs: true },
        baseUrlPattern: "localhost:8000",
      });
      expect(inst.instrumentationName).toBe("@traceai/vllm");
    });

    it("should accept RegExp baseUrlPattern", () => {
      const inst = new VLLMInstrumentation({
        baseUrlPattern: /localhost:\d+/,
      });
      expect(inst.instrumentationName).toBe("@traceai/vllm");
    });
  });

  describe("isPatched", () => {
    it("should return a boolean", () => {
      expect(typeof isPatched()).toBe("boolean");
    });
  });

  describe("manuallyInstrument", () => {
    it("should patch a mock OpenAI module", () => {
      const mockCreate = jest.fn().mockResolvedValue({
        id: "test-id",
        object: "chat.completion",
        created: Date.now(),
        model: "meta-llama/Llama-2-7b-chat-hf",
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
        OpenAI: class OpenAI {
          baseURL = "http://localhost:8000/v1";
          chat = {
            completions: {
              create: mockCreate,
            },
          };
          completions = {
            create: jest.fn(),
          };
        },
      };

      (mockModule.OpenAI.prototype as unknown as Record<string, unknown>).chat = {
        completions: { create: mockCreate },
      };
      (mockModule.OpenAI.prototype as unknown as Record<string, unknown>).completions = {
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
        model: "meta-llama/Llama-2-7b-chat-hf",
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
        createChatCompletionWrapper: (inst: VLLMInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createChatCompletionWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal);
    });

    it("should create a span for chat completions", async () => {
      const body = {
        model: "meta-llama/Llama-2-7b-chat-hf",
        messages: [
          { role: "user", content: "Hello!" },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("vLLM Chat Completions");
    });

    it("should set correct attributes for chat completion", async () => {
      const body = {
        model: "meta-llama/Llama-2-7b-chat-hf",
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
      expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("meta-llama/Llama-2-7b-chat-hf");
      expect(span.attributes[SemanticConventions.LLM_PROVIDER]).toBe(LLMProvider.VLLM);
      expect(span.attributes[SemanticConventions.INPUT_MIME_TYPE]).toBe(MimeType.JSON);
      expect(span.attributes[SemanticConventions.OUTPUT_MIME_TYPE]).toBe(MimeType.JSON);
    });

    it("should capture input messages", async () => {
      const body = {
        model: "meta-llama/Llama-2-7b-chat-hf",
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
        model: "meta-llama/Llama-2-7b-chat-hf",
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
        model: "meta-llama/Llama-2-7b-chat-hf",
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
        model: "meta-llama/Llama-2-7b-chat-hf",
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
        model: "meta-llama/Llama-2-7b-chat-hf",
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

      const outputMessages = JSON.parse(span.attributes[SemanticConventions.LLM_OUTPUT_MESSAGES] as string);
      expect(outputMessages[0].tool_calls[0].id).toBe("call_123");
      expect(outputMessages[0].tool_calls[0].function.name).toBe("get_weather");
    });

    it("should handle errors gracefully", async () => {
      const error = new Error("API Error");
      mockOriginal.mockRejectedValueOnce(error);

      const body = {
        model: "meta-llama/Llama-2-7b-chat-hf",
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
        model: "meta-llama/Llama-2-7b-chat-hf",
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
        model: "meta-llama/Llama-2-7b-hf",
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
        createCompletionWrapper: (inst: VLLMInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createCompletionWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal);
    });

    it("should create a span for completions", async () => {
      const body = {
        model: "meta-llama/Llama-2-7b-hf",
        prompt: "The quick brown fox",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("vLLM Completions");
    });

    it("should set correct attributes for completion", async () => {
      const body = {
        model: "meta-llama/Llama-2-7b-hf",
        prompt: "The quick brown fox",
        temperature: 0.5,
        max_tokens: 50,
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
      expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("meta-llama/Llama-2-7b-hf");
      expect(span.attributes[SemanticConventions.LLM_PROVIDER]).toBe(LLMProvider.VLLM);
      expect(span.attributes[`${SemanticConventions.LLM_PROMPTS}.0`]).toBe("The quick brown fox");
    });

    it("should capture output text", async () => {
      const body = {
        model: "meta-llama/Llama-2-7b-hf",
        prompt: "The quick brown fox",
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.OUTPUT_VALUE]).toBe(" jumped over the lazy dog.");
      expect(span.attributes[SemanticConventions.OUTPUT_MIME_TYPE]).toBe(MimeType.TEXT);
    });

    it("should handle errors gracefully", async () => {
      const error = new Error("Completion error");
      mockOriginal.mockRejectedValueOnce(error);

      const body = {
        model: "meta-llama/Llama-2-7b-hf",
        prompt: "Hello",
      };

      await expect(patchedFn(body)).rejects.toThrow("Completion error");

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
        createChatCompletionWrapper: (inst: VLLMInstrumentation) => (original: jest.Mock) => typeof patchedFn;
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
          model: "meta-llama/Llama-2-7b-chat-hf",
          choices: [{ index: 0, delta: { content: "Hello" }, finish_reason: null }],
        },
        {
          id: "chatcmpl-123",
          object: "chat.completion.chunk",
          created: 1677652288,
          model: "meta-llama/Llama-2-7b-chat-hf",
          choices: [{ index: 0, delta: { content: " world" }, finish_reason: null }],
        },
        {
          id: "chatcmpl-123",
          object: "chat.completion.chunk",
          created: 1677652288,
          model: "meta-llama/Llama-2-7b-chat-hf",
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
        model: "meta-llama/Llama-2-7b-chat-hf",
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
        model: "meta-llama/Llama-2-7b-chat-hf",
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

  describe("baseUrlPattern filtering", () => {
    it("should trace all requests when no pattern is set", async () => {
      const inst = new VLLMInstrumentation();
      const isVLLMRequest = (inst as unknown as { isVLLMRequest: (client: unknown) => boolean }).isVLLMRequest;

      expect(isVLLMRequest.call(inst, { baseURL: "http://example.com" })).toBe(true);
    });

    it("should filter by string pattern", async () => {
      const inst = new VLLMInstrumentation({ baseUrlPattern: "localhost:8000" });
      const isVLLMRequest = (inst as unknown as { isVLLMRequest: (client: unknown) => boolean }).isVLLMRequest;

      expect(isVLLMRequest.call(inst, { baseURL: "http://localhost:8000/v1" })).toBe(true);
      expect(isVLLMRequest.call(inst, { baseURL: "http://api.openai.com/v1" })).toBe(false);
    });

    it("should filter by RegExp pattern", async () => {
      const inst = new VLLMInstrumentation({ baseUrlPattern: /localhost:\d+/ });
      const isVLLMRequest = (inst as unknown as { isVLLMRequest: (client: unknown) => boolean }).isVLLMRequest;

      expect(isVLLMRequest.call(inst, { baseURL: "http://localhost:8000/v1" })).toBe(true);
      expect(isVLLMRequest.call(inst, { baseURL: "http://localhost:8001/v1" })).toBe(true);
      expect(isVLLMRequest.call(inst, { baseURL: "http://api.openai.com/v1" })).toBe(false);
    });
  });
});
