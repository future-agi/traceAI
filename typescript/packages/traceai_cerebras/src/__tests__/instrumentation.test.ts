import { CerebrasInstrumentation, isPatched } from "../instrumentation";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SemanticConventions, FISpanKind, MimeType, LLMSystem, LLMProvider } from "@traceai/fi-semantic-conventions";

describe("CerebrasInstrumentation", () => {
  let instrumentation: CerebrasInstrumentation;
  let memoryExporter: InMemorySpanExporter;
  let provider: NodeTracerProvider;

  beforeEach(() => {
    memoryExporter = new InMemorySpanExporter();
    provider = new NodeTracerProvider({
      spanProcessors: [new SimpleSpanProcessor(memoryExporter)],
    });
    provider.register();

    instrumentation = new CerebrasInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
  });

  afterEach(async () => {
    memoryExporter.reset();
    await provider.shutdown();
  });

  describe("constructor", () => {
    it("should create instrumentation with default config", () => {
      const inst = new CerebrasInstrumentation();
      expect(inst.instrumentationName).toBe("@traceai/cerebras");
    });

    it("should create instrumentation with custom config", () => {
      const inst = new CerebrasInstrumentation({
        instrumentationConfig: { enabled: true },
        traceConfig: { hideInputs: true },
      });
      expect(inst.instrumentationName).toBe("@traceai/cerebras");
    });
  });

  describe("isPatched", () => {
    it("should return a boolean", () => {
      expect(typeof isPatched()).toBe("boolean");
    });
  });

  describe("manuallyInstrument", () => {
    it("should patch a mock Cerebras module", () => {
      const mockCreate = jest.fn().mockResolvedValue({
        id: "test-id",
        object: "chat.completion",
        created: Date.now(),
        model: "llama3.1-8b",
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
        Cerebras: class Cerebras {
          chat = {
            completions: {
              create: mockCreate,
            },
          };
        },
      };

      (mockModule.Cerebras.prototype as unknown as Record<string, unknown>).chat = {
        completions: { create: mockCreate },
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
        model: "llama3.1-8b",
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
        time_info: {
          queue_time: 0.001,
          prompt_time: 0.015,
          completion_time: 0.045,
          total_time: 0.061,
        },
      });

      const wrapperFactory = (instrumentation as unknown as {
        createChatCompletionWrapper: (inst: CerebrasInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createChatCompletionWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal);
    });

    it("should create a span for chat completions", async () => {
      const body = {
        model: "llama3.1-8b",
        messages: [
          { role: "user", content: "Hello!" },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("Cerebras Chat Completions");
    });

    it("should set correct attributes for chat completion", async () => {
      const body = {
        model: "llama3.1-8b",
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
      expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("llama3.1-8b");
      expect(span.attributes[SemanticConventions.LLM_PROVIDER]).toBe(LLMProvider.CEREBRAS);
      expect(span.attributes[SemanticConventions.INPUT_MIME_TYPE]).toBe(MimeType.JSON);
    });

    it("should capture input messages", async () => {
      const body = {
        model: "llama3.1-8b",
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

    it("should capture output messages", async () => {
      const body = {
        model: "llama3.1-8b",
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
        model: "llama3.1-8b",
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

    it("should capture Cerebras-specific time_info metrics", async () => {
      const body = {
        model: "llama3.1-8b",
        messages: [
          { role: "user", content: "Hello!" },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes["cerebras.queue_time"]).toBe(0.001);
      expect(span.attributes["cerebras.prompt_time"]).toBe(0.015);
      expect(span.attributes["cerebras.completion_time"]).toBe(0.045);
      expect(span.attributes["cerebras.total_time"]).toBe(0.061);
    });

    it("should handle responses without time_info", async () => {
      mockOriginal.mockResolvedValueOnce({
        id: "chatcmpl-123",
        object: "chat.completion",
        created: 1677652288,
        model: "llama3.1-8b",
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
          prompt_tokens: 10,
          completion_tokens: 5,
          total_tokens: 15,
        },
        // No time_info
      });

      const body = {
        model: "llama3.1-8b",
        messages: [
          { role: "user", content: "Hello!" },
        ],
      };

      await patchedFn(body);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      // Should not have time_info attributes
      expect(span.attributes["cerebras.queue_time"]).toBeUndefined();
      expect(span.attributes["cerebras.total_time"]).toBeUndefined();
    });

    it("should handle errors gracefully", async () => {
      const error = new Error("API Error");
      mockOriginal.mockRejectedValueOnce(error);

      const body = {
        model: "llama3.1-8b",
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
        model: "llama3.1-8b",
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

  describe("streaming response handling", () => {
    let mockOriginal: jest.Mock;
    let patchedFn: (body: Record<string, unknown>, options?: Record<string, unknown>) => Promise<unknown>;

    beforeEach(() => {
      const wrapperFactory = (instrumentation as unknown as {
        createChatCompletionWrapper: (inst: CerebrasInstrumentation) => (original: jest.Mock) => typeof patchedFn;
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
          model: "llama3.1-8b",
          choices: [{ index: 0, delta: { content: "Hello" }, finish_reason: null }],
        },
        {
          id: "chatcmpl-123",
          object: "chat.completion.chunk",
          created: 1677652288,
          model: "llama3.1-8b",
          choices: [{ index: 0, delta: { content: " world" }, finish_reason: null }],
        },
        {
          id: "chatcmpl-123",
          object: "chat.completion.chunk",
          created: 1677652288,
          model: "llama3.1-8b",
          choices: [{ index: 0, delta: { content: "!" }, finish_reason: "stop" }],
          usage: { prompt_tokens: 5, completion_tokens: 3, total_tokens: 8 },
          time_info: {
            queue_time: 0.001,
            prompt_time: 0.01,
            completion_time: 0.03,
            total_time: 0.041,
          },
        },
      ];

      async function* mockStream() {
        for (const chunk of chunks) {
          yield chunk;
        }
      }

      mockOriginal.mockResolvedValueOnce(mockStream());

      const body = {
        model: "llama3.1-8b",
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

      // Time info from last chunk
      expect(span.attributes["cerebras.queue_time"]).toBe(0.001);
      expect(span.attributes["cerebras.total_time"]).toBe(0.041);
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
        model: "llama3.1-8b",
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
        model: "llama3.1-8b",
        choices: [
          {
            index: 0,
            message: { role: "assistant", content: "Response" },
            finish_reason: "stop",
          },
        ],
      });

      const wrapperFactory = (instrumentation as unknown as {
        createChatCompletionWrapper: (inst: CerebrasInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createChatCompletionWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal);
    });

    it("should capture invocation parameters", async () => {
      const body = {
        model: "llama3.1-8b",
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
