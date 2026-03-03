/**
 * Tests for Portkey instrumentation.
 */

import { PortkeyInstrumentation, isPatched, _resetPatchedStateForTesting } from "../instrumentation";
import { VERSION } from "../version";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";

describe("PortkeyInstrumentation", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  const instrumentation = new PortkeyInstrumentation();
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

  describe("PortkeyInstrumentation", () => {
    it("should create instrumentation instance", () => {
      const inst = new PortkeyInstrumentation();
      expect(inst).toBeInstanceOf(PortkeyInstrumentation);
    });

    it("should accept configuration options", () => {
      const inst = new PortkeyInstrumentation({
        traceConfig: {
          hideInputs: true,
          hideOutputs: true,
        },
      });
      expect(inst).toBeInstanceOf(PortkeyInstrumentation);
    });

    it("should have instrumentationName", () => {
      const inst = new PortkeyInstrumentation();
      expect(inst.instrumentationName).toBe("@traceai/fi-instrumentation-portkey");
    });

    it("should have instrumentationVersion", () => {
      const inst = new PortkeyInstrumentation();
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
      const inst = new PortkeyInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();

      // Should not throw
      inst.manuallyInstrument(null);
      expect(true).toBe(true);
    });

    it("should mark module as patched after manual instrumentation", () => {
      const inst = new PortkeyInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();

      const mockModule = { Portkey: {} };
      inst.manuallyInstrument(mockModule);
      expect(mockModule).toHaveProperty("_fiPatched", true);
    });
  });

  describe("enable/disable", () => {
    it("should enable instrumentation", () => {
      const inst = new PortkeyInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();
      // Should not throw
      expect(true).toBe(true);
    });

    it("should disable instrumentation", () => {
      const inst = new PortkeyInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();
      inst.disable();
      // Should not throw
      expect(true).toBe(true);
    });
  });
});

describe("Portkey Patching", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  beforeEach(() => {
    memoryExporter.reset();
    _resetPatchedStateForTesting();
  });

  it("should wrap chat.completions.create when available", () => {
    const inst = new PortkeyInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalCreate = jest.fn().mockResolvedValue({
      choices: [{ message: { role: "assistant", content: "Hello" } }],
      usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
    });

    const mockModule = {
      Portkey: {
        prototype: {
          chat: {
            completions: {
              create: originalCreate,
            },
          },
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should wrap completions.create when available", () => {
    const inst = new PortkeyInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalCreate = jest.fn().mockResolvedValue({
      choices: [{ text: "Hello world" }],
    });

    const mockModule = {
      Portkey: {
        prototype: {
          completions: {
            create: originalCreate,
          },
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should wrap embeddings.create when available", () => {
    const inst = new PortkeyInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalCreate = jest.fn().mockResolvedValue({
      data: [{ embedding: [0.1, 0.2, 0.3] }],
      usage: { prompt_tokens: 5, total_tokens: 5 },
    });

    const mockModule = {
      Portkey: {
        prototype: {
          embeddings: {
            create: originalCreate,
          },
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should not double-patch module", () => {
    const inst = new PortkeyInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const mockModule = {
      _fiPatched: true,
      Portkey: {
        prototype: {
          chat: {
            completions: {
              create: jest.fn(),
            },
          },
        },
      },
    };

    const original = mockModule.Portkey.prototype.chat.completions.create;
    inst.manuallyInstrument(mockModule);

    // Should not have changed
    expect(mockModule.Portkey.prototype.chat.completions.create).toBe(original);
  });
});
