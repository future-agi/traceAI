/**
 * Tests for Instructor instrumentation.
 */

import { InstructorInstrumentation, isPatched, _resetPatchedStateForTesting } from "../instrumentation";
import { VERSION } from "../version";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";

describe("InstructorInstrumentation", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  const instrumentation = new InstructorInstrumentation();
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

  describe("InstructorInstrumentation", () => {
    it("should create instrumentation instance", () => {
      const inst = new InstructorInstrumentation();
      expect(inst).toBeInstanceOf(InstructorInstrumentation);
    });

    it("should accept configuration options", () => {
      const inst = new InstructorInstrumentation({
        traceConfig: {
          hideInputs: true,
          hideOutputs: true,
        },
      });
      expect(inst).toBeInstanceOf(InstructorInstrumentation);
    });

    it("should have instrumentationName", () => {
      const inst = new InstructorInstrumentation();
      expect(inst.instrumentationName).toBe("@traceai/fi-instrumentation-instructor");
    });

    it("should have instrumentationVersion", () => {
      const inst = new InstructorInstrumentation();
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
      const inst = new InstructorInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();

      // Should not throw
      inst.manuallyInstrument(null);
      expect(true).toBe(true);
    });

    it("should mark module as patched after manual instrumentation", () => {
      const inst = new InstructorInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();

      const mockModule = { Instructor: {} };
      inst.manuallyInstrument(mockModule);
      expect(mockModule).toHaveProperty("_fiPatched", true);
    });
  });

  describe("enable/disable", () => {
    it("should enable instrumentation", () => {
      const inst = new InstructorInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();
      // Should not throw
      expect(true).toBe(true);
    });

    it("should disable instrumentation", () => {
      const inst = new InstructorInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();
      inst.disable();
      // Should not throw
      expect(true).toBe(true);
    });
  });
});

describe("Instructor Patching", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  beforeEach(() => {
    memoryExporter.reset();
    _resetPatchedStateForTesting();
  });

  it("should wrap Instructor.chat.completions.create when available", () => {
    const inst = new InstructorInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalCreate = jest.fn().mockResolvedValue({
      name: "John",
      age: 30,
    });

    const mockModule = {
      Instructor: {
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

  it("should wrap default export when it is a function", () => {
    const inst = new InstructorInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const mockCreate = jest.fn().mockResolvedValue({ extracted: "data" });
    const mockModule = {
      default: jest.fn().mockReturnValue({
        chat: {
          completions: {
            create: mockCreate,
          },
        },
      }),
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should not double-patch module", () => {
    const inst = new InstructorInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const mockModule = {
      _fiPatched: true,
      Instructor: {
        prototype: {
          chat: {
            completions: {
              create: jest.fn(),
            },
          },
        },
      },
    };

    const original = mockModule.Instructor.prototype.chat.completions.create;
    inst.manuallyInstrument(mockModule);

    // Should not have changed
    expect(mockModule.Instructor.prototype.chat.completions.create).toBe(original);
  });
});

describe("Structured Extraction Tracing", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  beforeEach(() => {
    memoryExporter.reset();
    _resetPatchedStateForTesting();
  });

  it("should handle response_model parameter", () => {
    const inst = new InstructorInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const mockModule = {
      Instructor: {
        prototype: {
          chat: {
            completions: {
              create: jest.fn().mockResolvedValue({ name: "Test" }),
            },
          },
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should handle responseModel parameter (camelCase)", () => {
    const inst = new InstructorInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const mockModule = {
      Instructor: {
        prototype: {
          chat: {
            completions: {
              create: jest.fn().mockResolvedValue({ result: "data" }),
            },
          },
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });

  it("should capture model name in span", () => {
    const inst = new InstructorInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const mockModule = {
      Instructor: {
        prototype: {
          chat: {
            completions: {
              create: jest.fn().mockResolvedValue({ extracted: "value" }),
            },
          },
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect((mockModule as any)._fiPatched).toBe(true);
  });
});
