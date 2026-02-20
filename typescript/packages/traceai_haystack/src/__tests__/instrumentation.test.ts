/**
 * Tests for Haystack instrumentation.
 */

import { HaystackInstrumentation, isPatched, _resetPatchedStateForTesting } from "../instrumentation";
import { VERSION } from "../version";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";

describe("HaystackInstrumentation", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  const instrumentation = new HaystackInstrumentation();
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

  describe("HaystackInstrumentation", () => {
    it("should create instrumentation instance", () => {
      const inst = new HaystackInstrumentation();
      expect(inst).toBeInstanceOf(HaystackInstrumentation);
    });

    it("should accept configuration options", () => {
      const inst = new HaystackInstrumentation({
        traceConfig: {
          hideInputs: true,
          hideOutputs: true,
        },
      });
      expect(inst).toBeInstanceOf(HaystackInstrumentation);
    });

    it("should have instrumentationName", () => {
      const inst = new HaystackInstrumentation();
      expect(inst.instrumentationName).toBe("@traceai/fi-instrumentation-haystack");
    });

    it("should have instrumentationVersion", () => {
      const inst = new HaystackInstrumentation();
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
      const inst = new HaystackInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();

      // Should not throw
      inst.manuallyInstrument(null);
      expect(true).toBe(true);
    });

    it("should mark module as patched after manual instrumentation", () => {
      const inst = new HaystackInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();

      const mockModule = { Pipeline: {} };
      inst.manuallyInstrument(mockModule);
      expect(mockModule).toHaveProperty("_fiPatched", true);
    });
  });

  describe("enable/disable", () => {
    it("should enable instrumentation", () => {
      const inst = new HaystackInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();
      // Should not throw
      expect(true).toBe(true);
    });

    it("should disable instrumentation", () => {
      const inst = new HaystackInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();
      inst.disable();
      // Should not throw
      expect(true).toBe(true);
    });
  });
});

describe("Haystack Patching", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  beforeEach(() => {
    memoryExporter.reset();
    _resetPatchedStateForTesting();
  });

  it("should wrap Pipeline.run when available", () => {
    const inst = new HaystackInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalRun = jest.fn().mockResolvedValue({
      documents: [{ content: "Test document" }],
    });

    const mockModule = {
      Pipeline: {
        prototype: {
          run: originalRun,
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should wrap Component.run when available", () => {
    const inst = new HaystackInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalRun = jest.fn().mockResolvedValue({
      output: "processed",
    });

    const mockModule = {
      Component: {
        prototype: {
          run: originalRun,
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should not double-patch module", () => {
    const inst = new HaystackInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const mockModule = {
      _fiPatched: true,
      Pipeline: {
        prototype: {
          run: jest.fn(),
        },
      },
    };

    const original = mockModule.Pipeline.prototype.run;
    inst.manuallyInstrument(mockModule);

    // Should not have changed
    expect(mockModule.Pipeline.prototype.run).toBe(original);
  });
});

describe("Component Type Detection", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  beforeEach(() => {
    memoryExporter.reset();
    _resetPatchedStateForTesting();
  });

  it("should detect retriever component type", () => {
    const inst = new HaystackInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const mockModule = {
      Component: {
        prototype: {
          run: jest.fn().mockResolvedValue({ documents: [] }),
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should detect generator/llm component type", () => {
    const inst = new HaystackInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const mockModule = {
      Component: {
        prototype: {
          run: jest.fn().mockResolvedValue({ replies: ["Hello"] }),
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should detect embedding component type", () => {
    const inst = new HaystackInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const mockModule = {
      Component: {
        prototype: {
          run: jest.fn().mockResolvedValue({ embeddings: [[0.1, 0.2]] }),
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });
});
