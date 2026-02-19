import { HuggingFaceInstrumentation, isPatched } from "../instrumentation";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SemanticConventions, FISpanKind, MimeType, LLMSystem, LLMProvider } from "@traceai/fi-semantic-conventions";

describe("HuggingFaceInstrumentation", () => {
  let instrumentation: HuggingFaceInstrumentation;
  let memoryExporter: InMemorySpanExporter;
  let provider: NodeTracerProvider;

  beforeEach(() => {
    memoryExporter = new InMemorySpanExporter();
    provider = new NodeTracerProvider();
    provider.addSpanProcessor(new SimpleSpanProcessor(memoryExporter));
    provider.register();

    instrumentation = new HuggingFaceInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
  });

  afterEach(async () => {
    memoryExporter.reset();
    await provider.shutdown();
  });

  describe("constructor", () => {
    it("should create instrumentation with default config", () => {
      const inst = new HuggingFaceInstrumentation();
      expect(inst.instrumentationName).toBe("@traceai/huggingface");
    });

    it("should create instrumentation with custom config", () => {
      const inst = new HuggingFaceInstrumentation({
        instrumentationConfig: { enabled: true },
        traceConfig: { hideInputs: true },
      });
      expect(inst.instrumentationName).toBe("@traceai/huggingface");
    });
  });

  describe("isPatched", () => {
    it("should return boolean", () => {
      expect(typeof isPatched()).toBe("boolean");
    });
  });

  describe("manuallyInstrument", () => {
    it("should patch a mock HuggingFace module", () => {
      const mockModule = {
        HfInference: class HfInference {
          textGeneration = jest.fn();
          chatCompletion = jest.fn();
          featureExtraction = jest.fn();
          summarization = jest.fn();
          translation = jest.fn();
          questionAnswering = jest.fn();
        },
      };

      // Set up prototype methods
      (mockModule.HfInference.prototype as unknown as Record<string, unknown>).textGeneration = jest.fn();
      (mockModule.HfInference.prototype as unknown as Record<string, unknown>).chatCompletion = jest.fn();
      (mockModule.HfInference.prototype as unknown as Record<string, unknown>).chatCompletionStream = jest.fn();
      (mockModule.HfInference.prototype as unknown as Record<string, unknown>).featureExtraction = jest.fn();
      (mockModule.HfInference.prototype as unknown as Record<string, unknown>).summarization = jest.fn();
      (mockModule.HfInference.prototype as unknown as Record<string, unknown>).translation = jest.fn();
      (mockModule.HfInference.prototype as unknown as Record<string, unknown>).questionAnswering = jest.fn();

      instrumentation.manuallyInstrument(mockModule as unknown as Record<string, unknown>);
      expect(isPatched()).toBe(true);
    });
  });

  describe("textGeneration wrapper", () => {
    let mockOriginal: jest.Mock;
    let patchedFn: (args: Record<string, unknown>) => Promise<unknown>;

    beforeEach(() => {
      mockOriginal = jest.fn().mockResolvedValue({
        generated_text: "The quick brown fox jumps over the lazy dog.",
      });

      const wrapperFactory = (instrumentation as unknown as {
        createTextGenerationWrapper: (inst: HuggingFaceInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createTextGenerationWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal);
    });

    it("should create a span for text generation", async () => {
      const args = {
        model: "gpt2",
        inputs: "The quick brown",
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("HuggingFace Text Generation");
    });

    it("should set correct attributes", async () => {
      const args = {
        model: "gpt2",
        inputs: "The quick brown",
        parameters: {
          max_new_tokens: 50,
          temperature: 0.7,
        },
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
      expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("gpt2");
      expect(span.attributes[SemanticConventions.LLM_SYSTEM]).toBe(LLMSystem.HUGGINGFACE);
      expect(span.attributes[SemanticConventions.LLM_PROVIDER]).toBe(LLMProvider.HUGGINGFACE);
      expect(span.attributes[SemanticConventions.INPUT_VALUE]).toBe("The quick brown");
      expect(span.attributes[SemanticConventions.INPUT_MIME_TYPE]).toBe(MimeType.TEXT);
    });

    it("should capture output", async () => {
      const args = {
        model: "gpt2",
        inputs: "The quick brown",
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.OUTPUT_VALUE]).toBe("The quick brown fox jumps over the lazy dog.");
      expect(span.attributes[SemanticConventions.OUTPUT_MIME_TYPE]).toBe(MimeType.TEXT);
    });

    it("should capture prompt", async () => {
      const args = {
        model: "gpt2",
        inputs: "The quick brown",
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[`${SemanticConventions.LLM_PROMPTS}.0`]).toBe("The quick brown");
    });

    it("should handle errors", async () => {
      const error = new Error("Text generation error");
      mockOriginal.mockRejectedValueOnce(error);

      const args = {
        model: "gpt2",
        inputs: "The quick brown",
      };

      await expect(patchedFn(args)).rejects.toThrow("Text generation error");

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].status.code).toBe(2);
    });

    it("should handle unknown model", async () => {
      const args = {
        inputs: "The quick brown",
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("unknown");
    });
  });

  describe("chatCompletion wrapper", () => {
    let mockOriginal: jest.Mock;
    let patchedFn: (args: Record<string, unknown>) => Promise<unknown>;

    beforeEach(() => {
      mockOriginal = jest.fn().mockResolvedValue({
        id: "chatcmpl-123",
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
          completion_tokens: 8,
          total_tokens: 18,
        },
      });

      const wrapperFactory = (instrumentation as unknown as {
        createChatCompletionWrapper: (inst: HuggingFaceInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createChatCompletionWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal);
    });

    it("should create a span for chat completion", async () => {
      const args = {
        model: "meta-llama/Llama-2-7b-chat-hf",
        messages: [
          { role: "user", content: "Hello!" },
        ],
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("HuggingFace Chat Completion");
    });

    it("should set correct attributes", async () => {
      const args = {
        model: "meta-llama/Llama-2-7b-chat-hf",
        messages: [
          { role: "system", content: "You are helpful." },
          { role: "user", content: "Hello!" },
        ],
        max_tokens: 100,
        temperature: 0.7,
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
      expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("meta-llama/Llama-2-7b-chat-hf");
      expect(span.attributes[SemanticConventions.LLM_SYSTEM]).toBe(LLMSystem.HUGGINGFACE);
      expect(span.attributes[SemanticConventions.INPUT_MIME_TYPE]).toBe(MimeType.JSON);
    });

    it("should capture input messages", async () => {
      const args = {
        model: "meta-llama/Llama-2-7b-chat-hf",
        messages: [
          { role: "system", content: "You are helpful." },
          { role: "user", content: "Hello!" },
        ],
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[`${SemanticConventions.LLM_INPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_ROLE}`]).toBe("system");
      expect(span.attributes[`${SemanticConventions.LLM_INPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_CONTENT}`]).toBe("You are helpful.");
      expect(span.attributes[`${SemanticConventions.LLM_INPUT_MESSAGES}.1.${SemanticConventions.MESSAGE_ROLE}`]).toBe("user");
      expect(span.attributes[`${SemanticConventions.LLM_INPUT_MESSAGES}.1.${SemanticConventions.MESSAGE_CONTENT}`]).toBe("Hello!");
    });

    it("should capture output messages", async () => {
      const args = {
        model: "meta-llama/Llama-2-7b-chat-hf",
        messages: [{ role: "user", content: "Hello!" }],
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[`${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_ROLE}`]).toBe("assistant");
      expect(span.attributes[`${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_CONTENT}`]).toBe("Hello! How can I help you today?");
    });

    it("should capture token usage", async () => {
      const args = {
        model: "meta-llama/Llama-2-7b-chat-hf",
        messages: [{ role: "user", content: "Hello!" }],
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT]).toBe(10);
      expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]).toBe(8);
      expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL]).toBe(18);
    });

    it("should handle errors", async () => {
      const error = new Error("Chat completion error");
      mockOriginal.mockRejectedValueOnce(error);

      const args = {
        model: "meta-llama/Llama-2-7b-chat-hf",
        messages: [{ role: "user", content: "Hello!" }],
      };

      await expect(patchedFn(args)).rejects.toThrow("Chat completion error");

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].status.code).toBe(2);
    });
  });

  describe("chatCompletionStream wrapper", () => {
    let mockOriginal: jest.Mock;
    let patchedFn: (args: Record<string, unknown>) => Promise<unknown>;

    beforeEach(() => {
      const wrapperFactory = (instrumentation as unknown as {
        createChatCompletionStreamWrapper: (inst: HuggingFaceInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createChatCompletionStreamWrapper(instrumentation);

      mockOriginal = jest.fn();
      patchedFn = wrapperFactory(mockOriginal);
    });

    it("should handle streaming responses", async () => {
      const chunks = [
        { choices: [{ index: 0, delta: { content: "Hello" } }] },
        { choices: [{ index: 0, delta: { content: " world" } }] },
        { choices: [{ index: 0, delta: { content: "!" }, finish_reason: "stop" }] },
      ];

      async function* mockStream() {
        for (const chunk of chunks) {
          yield chunk;
        }
      }

      mockOriginal.mockResolvedValueOnce(mockStream());

      const args = {
        model: "meta-llama/Llama-2-7b-chat-hf",
        messages: [{ role: "user", content: "Hello!" }],
        stream: true,
      };

      const result = await patchedFn(args);
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
        yield { choices: [{ index: 0, delta: { content: "Hello" } }] };
        throw new Error("Stream error");
      }

      mockOriginal.mockResolvedValueOnce(mockErrorStream());

      const args = {
        model: "meta-llama/Llama-2-7b-chat-hf",
        messages: [{ role: "user", content: "Hello!" }],
        stream: true,
      };

      const result = await patchedFn(args);

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

  describe("featureExtraction wrapper", () => {
    let mockOriginal: jest.Mock;
    let patchedFn: (args: Record<string, unknown>) => Promise<unknown>;

    beforeEach(() => {
      mockOriginal = jest.fn().mockResolvedValue([0.1, 0.2, 0.3, 0.4, 0.5]);

      const wrapperFactory = (instrumentation as unknown as {
        createFeatureExtractionWrapper: (inst: HuggingFaceInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createFeatureExtractionWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal);
    });

    it("should create a span for feature extraction", async () => {
      const args = {
        model: "sentence-transformers/all-MiniLM-L6-v2",
        inputs: "Hello world",
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("HuggingFace Feature Extraction");
    });

    it("should set correct attributes", async () => {
      const args = {
        model: "sentence-transformers/all-MiniLM-L6-v2",
        inputs: "Hello world",
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.EMBEDDING);
      expect(span.attributes[SemanticConventions.EMBEDDING_MODEL_NAME]).toBe("sentence-transformers/all-MiniLM-L6-v2");
      expect(span.attributes[SemanticConventions.LLM_SYSTEM]).toBe(LLMSystem.HUGGINGFACE);
    });

    it("should capture input text", async () => {
      const args = {
        model: "sentence-transformers/all-MiniLM-L6-v2",
        inputs: "Hello world",
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_TEXT}`]).toBe("Hello world");
    });

    it("should capture embedding vector", async () => {
      const args = {
        model: "sentence-transformers/all-MiniLM-L6-v2",
        inputs: "Hello world",
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      const vectorAttr = span.attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_VECTOR}`];
      expect(vectorAttr).toBeDefined();
      expect(JSON.parse(vectorAttr as string)).toEqual([0.1, 0.2, 0.3, 0.4, 0.5]);
    });

    it("should handle batch inputs", async () => {
      mockOriginal.mockResolvedValueOnce([
        [0.1, 0.2],
        [0.3, 0.4],
      ]);

      const args = {
        model: "sentence-transformers/all-MiniLM-L6-v2",
        inputs: ["Hello", "World"],
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_TEXT}`]).toBe("Hello");
      expect(span.attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.1.${SemanticConventions.EMBEDDING_TEXT}`]).toBe("World");
    });

    it("should handle errors", async () => {
      const error = new Error("Feature extraction error");
      mockOriginal.mockRejectedValueOnce(error);

      const args = {
        model: "sentence-transformers/all-MiniLM-L6-v2",
        inputs: "Hello world",
      };

      await expect(patchedFn(args)).rejects.toThrow("Feature extraction error");

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].status.code).toBe(2);
    });
  });

  describe("summarization wrapper", () => {
    let mockOriginal: jest.Mock;
    let patchedFn: (args: Record<string, unknown>) => Promise<unknown>;

    beforeEach(() => {
      mockOriginal = jest.fn().mockResolvedValue({
        summary_text: "This is a summary of the input text.",
      });

      const wrapperFactory = (instrumentation as unknown as {
        createSummarizationWrapper: (inst: HuggingFaceInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createSummarizationWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal);
    });

    it("should create a span for summarization", async () => {
      const args = {
        model: "facebook/bart-large-cnn",
        inputs: "Long text to summarize...",
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("HuggingFace Summarization");
    });

    it("should set correct attributes", async () => {
      const args = {
        model: "facebook/bart-large-cnn",
        inputs: "Long text to summarize...",
        parameters: {
          max_length: 100,
          min_length: 30,
        },
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
      expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("facebook/bart-large-cnn");
      expect(span.attributes[SemanticConventions.INPUT_VALUE]).toBe("Long text to summarize...");
      expect(span.attributes[SemanticConventions.INPUT_MIME_TYPE]).toBe(MimeType.TEXT);
    });

    it("should capture output", async () => {
      const args = {
        model: "facebook/bart-large-cnn",
        inputs: "Long text to summarize...",
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.OUTPUT_VALUE]).toBe("This is a summary of the input text.");
      expect(span.attributes[SemanticConventions.OUTPUT_MIME_TYPE]).toBe(MimeType.TEXT);
    });

    it("should handle errors", async () => {
      const error = new Error("Summarization error");
      mockOriginal.mockRejectedValueOnce(error);

      const args = {
        model: "facebook/bart-large-cnn",
        inputs: "Long text to summarize...",
      };

      await expect(patchedFn(args)).rejects.toThrow("Summarization error");

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].status.code).toBe(2);
    });
  });

  describe("translation wrapper", () => {
    let mockOriginal: jest.Mock;
    let patchedFn: (args: Record<string, unknown>) => Promise<unknown>;

    beforeEach(() => {
      mockOriginal = jest.fn().mockResolvedValue({
        translation_text: "Bonjour le monde!",
      });

      const wrapperFactory = (instrumentation as unknown as {
        createTranslationWrapper: (inst: HuggingFaceInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createTranslationWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal);
    });

    it("should create a span for translation", async () => {
      const args = {
        model: "Helsinki-NLP/opus-mt-en-fr",
        inputs: "Hello world!",
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("HuggingFace Translation");
    });

    it("should set correct attributes", async () => {
      const args = {
        model: "Helsinki-NLP/opus-mt-en-fr",
        inputs: "Hello world!",
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
      expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("Helsinki-NLP/opus-mt-en-fr");
      expect(span.attributes[SemanticConventions.INPUT_VALUE]).toBe("Hello world!");
      expect(span.attributes[SemanticConventions.INPUT_MIME_TYPE]).toBe(MimeType.TEXT);
    });

    it("should capture output", async () => {
      const args = {
        model: "Helsinki-NLP/opus-mt-en-fr",
        inputs: "Hello world!",
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.OUTPUT_VALUE]).toBe("Bonjour le monde!");
      expect(span.attributes[SemanticConventions.OUTPUT_MIME_TYPE]).toBe(MimeType.TEXT);
    });

    it("should handle errors", async () => {
      const error = new Error("Translation error");
      mockOriginal.mockRejectedValueOnce(error);

      const args = {
        model: "Helsinki-NLP/opus-mt-en-fr",
        inputs: "Hello world!",
      };

      await expect(patchedFn(args)).rejects.toThrow("Translation error");

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].status.code).toBe(2);
    });
  });

  describe("questionAnswering wrapper", () => {
    let mockOriginal: jest.Mock;
    let patchedFn: (args: Record<string, unknown>) => Promise<unknown>;

    beforeEach(() => {
      mockOriginal = jest.fn().mockResolvedValue({
        answer: "Paris",
        score: 0.95,
        start: 0,
        end: 5,
      });

      const wrapperFactory = (instrumentation as unknown as {
        createQuestionAnsweringWrapper: (inst: HuggingFaceInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createQuestionAnsweringWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal);
    });

    it("should create a span for question answering", async () => {
      const args = {
        model: "deepset/roberta-base-squad2",
        inputs: {
          question: "What is the capital of France?",
          context: "Paris is the capital and most populous city of France.",
        },
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("HuggingFace Question Answering");
    });

    it("should set correct attributes", async () => {
      const args = {
        model: "deepset/roberta-base-squad2",
        inputs: {
          question: "What is the capital of France?",
          context: "Paris is the capital and most populous city of France.",
        },
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
      expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("deepset/roberta-base-squad2");
      expect(span.attributes[SemanticConventions.INPUT_MIME_TYPE]).toBe(MimeType.JSON);
    });

    it("should capture output", async () => {
      const args = {
        model: "deepset/roberta-base-squad2",
        inputs: {
          question: "What is the capital of France?",
          context: "Paris is the capital and most populous city of France.",
        },
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.OUTPUT_VALUE]).toBe("Paris");
      expect(span.attributes[SemanticConventions.OUTPUT_MIME_TYPE]).toBe(MimeType.TEXT);
    });

    it("should handle errors", async () => {
      const error = new Error("QA error");
      mockOriginal.mockRejectedValueOnce(error);

      const args = {
        model: "deepset/roberta-base-squad2",
        inputs: {
          question: "What is the capital of France?",
          context: "Paris is the capital and most populous city of France.",
        },
      };

      await expect(patchedFn(args)).rejects.toThrow("QA error");

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].status.code).toBe(2);
    });
  });

  describe("raw input/output capture", () => {
    let mockOriginal: jest.Mock;
    let patchedFn: (args: Record<string, unknown>) => Promise<unknown>;

    beforeEach(() => {
      mockOriginal = jest.fn().mockResolvedValue({
        generated_text: "Output text",
      });

      const wrapperFactory = (instrumentation as unknown as {
        createTextGenerationWrapper: (inst: HuggingFaceInstrumentation) => (original: jest.Mock) => typeof patchedFn;
      }).createTextGenerationWrapper(instrumentation);
      patchedFn = wrapperFactory(mockOriginal);
    });

    it("should capture raw input and output", async () => {
      const args = {
        model: "gpt2",
        inputs: "Hello",
        parameters: { temperature: 0.7 },
      };

      await patchedFn(args);

      const spans = memoryExporter.getFinishedSpans();
      const span = spans[0];

      expect(span.attributes[SemanticConventions.RAW_INPUT]).toBeDefined();
      expect(span.attributes[SemanticConventions.RAW_OUTPUT]).toBeDefined();
    });
  });

  describe("InferenceClient patching", () => {
    it("should also patch InferenceClient if present", () => {
      const mockModule = {
        HfInference: class HfInference {
          textGeneration = jest.fn();
        },
        InferenceClient: class InferenceClient {
          textGeneration = jest.fn();
          chatCompletion = jest.fn();
        },
      };

      // Set up HfInference prototype
      (mockModule.HfInference.prototype as unknown as Record<string, unknown>).textGeneration = jest.fn();

      // Set up InferenceClient prototype
      (mockModule.InferenceClient.prototype as unknown as Record<string, unknown>).textGeneration = jest.fn();
      (mockModule.InferenceClient.prototype as unknown as Record<string, unknown>).chatCompletion = jest.fn();
      (mockModule.InferenceClient.prototype as unknown as Record<string, unknown>).chatCompletionStream = jest.fn();
      (mockModule.InferenceClient.prototype as unknown as Record<string, unknown>).featureExtraction = jest.fn();
      (mockModule.InferenceClient.prototype as unknown as Record<string, unknown>).summarization = jest.fn();
      (mockModule.InferenceClient.prototype as unknown as Record<string, unknown>).translation = jest.fn();
      (mockModule.InferenceClient.prototype as unknown as Record<string, unknown>).questionAnswering = jest.fn();

      instrumentation.manuallyInstrument(mockModule as unknown as Record<string, unknown>);
      expect(isPatched()).toBe(true);
    });
  });
});
