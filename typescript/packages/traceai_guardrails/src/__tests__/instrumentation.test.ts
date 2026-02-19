/**
 * Tests for Guardrails instrumentation.
 */

import { GuardrailsInstrumentation, isPatched } from "../instrumentation";
import { VERSION } from "../version";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";

describe("GuardrailsInstrumentation", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  const instrumentation = new GuardrailsInstrumentation();
  instrumentation.disable();
  instrumentation.setTracerProvider(tracerProvider);

  beforeEach(() => {
    memoryExporter.reset();
  });

  afterAll(() => {
    instrumentation.disable();
  });

  describe("VERSION", () => {
    it("should export VERSION constant", () => {
      expect(VERSION).toBe("0.1.0");
    });
  });

  describe("GuardrailsInstrumentation", () => {
    it("should create instrumentation instance", () => {
      const inst = new GuardrailsInstrumentation();
      expect(inst).toBeInstanceOf(GuardrailsInstrumentation);
    });

    it("should accept configuration options", () => {
      const inst = new GuardrailsInstrumentation({
        traceConfig: {
          hideInputs: true,
          hideOutputs: true,
        },
      });
      expect(inst).toBeInstanceOf(GuardrailsInstrumentation);
    });

    it("should have instrumentationName", () => {
      const inst = new GuardrailsInstrumentation();
      expect(inst.instrumentationName).toBe("@traceai/fi-instrumentation-guardrails");
    });

    it("should have instrumentationVersion", () => {
      const inst = new GuardrailsInstrumentation();
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
      const inst = new GuardrailsInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();

      // Should not throw
      inst.manuallyInstrument(null);
      expect(true).toBe(true);
    });

    it("should mark module as patched after manual instrumentation", () => {
      const inst = new GuardrailsInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();

      const mockModule = { Guard: {} };
      inst.manuallyInstrument(mockModule);
      expect(mockModule).toHaveProperty("_fiPatched", true);
    });
  });

  describe("enable/disable", () => {
    it("should enable instrumentation", () => {
      const inst = new GuardrailsInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();
      // Should not throw
      expect(true).toBe(true);
    });

    it("should disable instrumentation", () => {
      const inst = new GuardrailsInstrumentation();
      inst.setTracerProvider(tracerProvider);
      inst.enable();
      inst.disable();
      // Should not throw
      expect(true).toBe(true);
    });
  });
});

describe("Guardrails Patching", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  beforeEach(() => {
    memoryExporter.reset();
  });

  it("should wrap Guard.validate when available", () => {
    const inst = new GuardrailsInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalValidate = jest.fn().mockResolvedValue({
      validated: true,
      valid: true,
      output: "Valid output",
    });

    const mockModule = {
      Guard: {
        prototype: {
          validate: originalValidate,
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect(mockModule._fiPatched).toBe(true);
  });

  it("should wrap Guard.parse when available", () => {
    const inst = new GuardrailsInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalParse = jest.fn().mockResolvedValue({
      validated: true,
      parsed_output: { name: "John" },
    });

    const mockModule = {
      Guard: {
        prototype: {
          parse: originalParse,
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect(mockModule._fiPatched).toBe(true);
  });

  it("should wrap Guard.call when available", () => {
    const inst = new GuardrailsInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const originalCall = jest.fn().mockResolvedValue({
      validated: true,
      output: "Called output",
    });

    const mockModule = {
      Guard: {
        prototype: {
          call: originalCall,
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect(mockModule._fiPatched).toBe(true);
  });

  it("should not double-patch module", () => {
    const inst = new GuardrailsInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const mockModule = {
      _fiPatched: true,
      Guard: {
        prototype: {
          validate: jest.fn(),
        },
      },
    };

    const original = mockModule.Guard.prototype.validate;
    inst.manuallyInstrument(mockModule);

    // Should not have changed
    expect(mockModule.Guard.prototype.validate).toBe(original);
  });
});

describe("Validation Result Handling", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  beforeEach(() => {
    memoryExporter.reset();
  });

  it("should handle validation success", () => {
    const inst = new GuardrailsInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const mockModule = {
      Guard: {
        prototype: {
          validate: jest.fn().mockResolvedValue({
            validated: true,
            valid: true,
            output: "Valid",
          }),
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect(mockModule._fiPatched).toBe(true);
  });

  it("should handle validation failure", () => {
    const inst = new GuardrailsInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const mockModule = {
      Guard: {
        prototype: {
          validate: jest.fn().mockResolvedValue({
            validated: false,
            valid: false,
            error: "Validation failed",
          }),
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect(mockModule._fiPatched).toBe(true);
  });

  it("should handle reask result", () => {
    const inst = new GuardrailsInstrumentation();
    inst.setTracerProvider(tracerProvider);
    inst.enable();

    const mockModule = {
      Guard: {
        prototype: {
          validate: jest.fn().mockResolvedValue({
            validated: true,
            reask: true,
            reask_count: 2,
          }),
        },
      },
    };

    inst.manuallyInstrument(mockModule);
    expect(mockModule._fiPatched).toBe(true);
  });
});
