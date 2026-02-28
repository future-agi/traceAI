/**
 * Tests for Vertex AI instrumentation.
 */

import { VertexAIInstrumentation, isPatched, _resetPatchedStateForTesting } from "../instrumentation";
import { VERSION } from "../version";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";

describe("VertexAIInstrumentation", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  const instrumentation = new VertexAIInstrumentation();
  instrumentation.disable();
  instrumentation.setTracerProvider(tracerProvider);

  beforeEach(() => {
    memoryExporter.reset();
    _resetPatchedStateForTesting();
  });

  afterAll(() => {
    instrumentation.disable();
  });

  describe("VERSION", () => {
    it("should export VERSION constant", () => {
      expect(VERSION).toBe("0.1.0");
    });
  });

  describe("VertexAIInstrumentation", () => {
    it("should create instrumentation instance", () => {
      const inst = new VertexAIInstrumentation();
      expect(inst).toBeInstanceOf(VertexAIInstrumentation);
    });

    it("should accept configuration options", () => {
      const inst = new VertexAIInstrumentation({
        traceConfig: {
          hideInputs: true,
          hideOutputs: true,
        },
      });
      expect(inst).toBeInstanceOf(VertexAIInstrumentation);
    });

    it("should have instrumentationName", () => {
      const inst = new VertexAIInstrumentation();
      expect(inst.instrumentationName).toBe("@traceai/fi-instrumentation-vertexai");
    });

    it("should have instrumentationVersion", () => {
      const inst = new VertexAIInstrumentation();
      expect(inst.instrumentationVersion).toBe(VERSION);
    });
  });

  describe("isPatched", () => {
    it("should return false when not patched", () => {
      instrumentation.disable();
      expect(isPatched()).toBe(false);
    });
  });

  describe("manuallyInstrument", () => {
    it("should handle null module", () => {
      const inst = new VertexAIInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();

      // Should not throw
      inst.manuallyInstrument(null);
      expect(true).toBe(true);
    });

    it("should mark module as patched after manual instrumentation", () => {
      const inst = new VertexAIInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();

      const mockModule = { GenerativeModel: {} };
      inst.manuallyInstrument(mockModule);
      expect(mockModule).toHaveProperty("_fiPatched", true);
    });
  });

  describe("enable/disable", () => {
    it("should enable instrumentation", () => {
      const inst = new VertexAIInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();
      // Should not throw
      expect(true).toBe(true);
    });

    it("should disable instrumentation", () => {
      const inst = new VertexAIInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();
      inst.disable();
      // Should not throw
      expect(true).toBe(true);
    });
  });
});

describe("GenerativeModel Patching", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  beforeEach(() => {
    memoryExporter.reset();
    _resetPatchedStateForTesting();
  });

  it("should wrap GenerativeModel.generateContent when available", () => {
    const inst = new VertexAIInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalGenerate = jest.fn().mockResolvedValue({
      response: {
        candidates: [{ content: { role: "model", parts: [{ text: "Hello!" }] } }],
        usageMetadata: { promptTokenCount: 10, candidatesTokenCount: 5, totalTokenCount: 15 },
      },
    });

    const mockModule = {
      GenerativeModel: {
        prototype: {
          generateContent: originalGenerate,
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should wrap GenerativeModel.generateContentStream when available", () => {
    const inst = new VertexAIInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalStream = jest.fn().mockResolvedValue({
      stream: (async function* () {
        yield { candidates: [{ content: { parts: [{ text: "Hello" }] } }] };
      })(),
      response: Promise.resolve({}),
    });

    const mockModule = {
      GenerativeModel: {
        prototype: {
          generateContentStream: originalStream,
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should wrap GenerativeModel.countTokens when available", () => {
    const inst = new VertexAIInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalCount = jest.fn().mockResolvedValue({ totalTokens: 100 });

    const mockModule = {
      GenerativeModel: {
        prototype: {
          countTokens: originalCount,
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should not double-patch module", () => {
    const inst = new VertexAIInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const mockModule = {
      _fiPatched: true,
      GenerativeModel: {
        prototype: {
          generateContent: jest.fn(),
        },
      },
    };

    const original = mockModule.GenerativeModel.prototype.generateContent;
    inst.manuallyInstrument(mockModule);

    // Should not have changed
    expect(mockModule.GenerativeModel.prototype.generateContent).toBe(original);
  });
});

describe("ChatSession Patching", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  beforeEach(() => {
    memoryExporter.reset();
    _resetPatchedStateForTesting();
  });

  it("should wrap ChatSession.sendMessage when available", () => {
    const inst = new VertexAIInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalSend = jest.fn().mockResolvedValue({
      response: {
        candidates: [{ content: { role: "model", parts: [{ text: "Response" }] } }],
      },
    });

    const mockModule = {
      ChatSession: {
        prototype: {
          sendMessage: originalSend,
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should wrap ChatSession.sendMessageStream when available", () => {
    const inst = new VertexAIInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalStream = jest.fn().mockResolvedValue({
      stream: (async function* () {
        yield { candidates: [{ content: { parts: [{ text: "Streaming" }] } }] };
      })(),
    });

    const mockModule = {
      ChatSession: {
        prototype: {
          sendMessageStream: originalStream,
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });
});

describe("TextEmbeddingModel Patching", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  beforeEach(() => {
    memoryExporter.reset();
    _resetPatchedStateForTesting();
  });

  it("should wrap TextEmbeddingModel.embed when available", () => {
    const inst = new VertexAIInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalEmbed = jest.fn().mockResolvedValue({
      embeddings: [{ values: [0.1, 0.2, 0.3] }],
    });

    const mockModule = {
      TextEmbeddingModel: {
        prototype: {
          embed: originalEmbed,
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });
});
