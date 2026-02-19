import { describe, it, expect, jest, beforeEach, afterEach } from "@jest/globals";
import { MistralInstrumentation, isPatched } from "../instrumentation";

// Mock the dependencies
jest.mock("@traceai/fi-core", () => ({
  FITracer: jest.fn().mockImplementation(() => ({
    startSpan: jest.fn().mockReturnValue({
      setStatus: jest.fn(),
      setAttribute: jest.fn(),
      recordException: jest.fn(),
      setAttributes: jest.fn(),
      addEvent: jest.fn(),
      end: jest.fn(),
      isRecording: jest.fn().mockReturnValue(true),
      spanContext: jest.fn().mockReturnValue({ spanId: "test-span-id" }),
    }),
    startActiveSpan: jest.fn((name: string, fn: any) => {
      const mockSpan = {
        setStatus: jest.fn(),
        recordException: jest.fn(),
        setAttributes: jest.fn(),
        addEvent: jest.fn(),
        end: jest.fn(),
        isRecording: jest.fn().mockReturnValue(true),
      };
      return fn(mockSpan);
    }),
  })),
  safelyJSONStringify: jest.fn((obj: any) => JSON.stringify(obj)),
  TraceConfigOptions: {},
}));

describe("Mistral Instrumentation", () => {
  let instrumentation: MistralInstrumentation;

  beforeEach(() => {
    jest.clearAllMocks();

    instrumentation = new MistralInstrumentation({
      instrumentationConfig: {
        enabled: true,
      },
    });

    // Mock the tracer property
    const mockTracer = {
      startSpan: jest.fn().mockReturnValue({
        setStatus: jest.fn(),
        recordException: jest.fn(),
        setAttributes: jest.fn(),
        addEvent: jest.fn(),
        end: jest.fn(),
        isRecording: jest.fn().mockReturnValue(true),
      }),
      startActiveSpan: jest.fn((name: string, fn: any) => {
        const mockSpan = {
          setStatus: jest.fn(),
          recordException: jest.fn(),
          setAttributes: jest.fn(),
          addEvent: jest.fn(),
          end: jest.fn(),
          isRecording: jest.fn().mockReturnValue(true),
        };
        return fn(mockSpan);
      }),
    };

    Object.defineProperty(instrumentation, "tracer", {
      get: () => mockTracer,
      configurable: true,
    });
  });

  afterEach(() => {
    instrumentation.disable();
  });

  describe("Initialization", () => {
    it("should create instrumentation instance", () => {
      expect(instrumentation).toBeInstanceOf(MistralInstrumentation);
    });

    it("should initialize with default config", () => {
      const defaultInstrumentation = new MistralInstrumentation();
      expect(defaultInstrumentation).toBeInstanceOf(MistralInstrumentation);
    });

    it("should initialize with custom instrumentation config", () => {
      const customInstrumentation = new MistralInstrumentation({
        instrumentationConfig: {
          enabled: false,
        },
      });
      expect(customInstrumentation).toBeInstanceOf(MistralInstrumentation);
    });

    it("should initialize with trace config", () => {
      const traceConfigInstrumentation = new MistralInstrumentation({
        traceConfig: {
          hideInputs: true,
          hideOutputs: true,
        },
      });
      expect(traceConfigInstrumentation).toBeInstanceOf(MistralInstrumentation);
    });

    it("should have correct instrumentation name", () => {
      expect(instrumentation.instrumentationName).toBe("@traceai/mistral");
    });
  });

  describe("isPatched function", () => {
    it("should be a function", () => {
      expect(typeof isPatched).toBe("function");
    });

    it("should return false initially", () => {
      expect(typeof isPatched()).toBe("boolean");
    });
  });

  describe("Instrumentation lifecycle", () => {
    it("should enable without errors", () => {
      expect(() => instrumentation.enable()).not.toThrow();
    });

    it("should disable without errors", () => {
      instrumentation.enable();
      expect(() => instrumentation.disable()).not.toThrow();
    });

    it("should handle multiple enable calls", () => {
      expect(() => {
        instrumentation.enable();
        instrumentation.enable();
      }).not.toThrow();
    });

    it("should handle multiple disable calls", () => {
      instrumentation.enable();
      expect(() => {
        instrumentation.disable();
        instrumentation.disable();
      }).not.toThrow();
    });
  });

  describe("manuallyInstrument", () => {
    it("should accept a mock module", () => {
      instrumentation.enable();

      const mockModule = {
        Mistral: {
          prototype: {
            chat: {
              complete: jest.fn(),
            },
            embeddings: {
              create: jest.fn(),
            },
          },
        },
      };

      expect(() => instrumentation.manuallyInstrument(mockModule)).not.toThrow();
    });

    it("should handle module without Mistral property", () => {
      instrumentation.enable();

      const mockModule = {};

      expect(() => instrumentation.manuallyInstrument(mockModule)).not.toThrow();
    });

    it("should handle module with partial structure", () => {
      instrumentation.enable();

      const mockModule = {
        Mistral: {
          prototype: {},
        },
      };

      expect(() => instrumentation.manuallyInstrument(mockModule)).not.toThrow();
    });
  });
});

describe("Mistral Instrumentation - Semantic Conventions", () => {
  it("should use correct LLM system identifier", () => {
    const { LLMSystem, LLMProvider } = require("@traceai/fi-semantic-conventions");
    expect(LLMSystem.MISTRALAI).toBe("mistralai");
    expect(LLMProvider.MISTRALAI).toBe("mistralai");
  });

  it("should use correct span kind for LLM operations", () => {
    const { FISpanKind } = require("@traceai/fi-semantic-conventions");
    expect(FISpanKind.LLM).toBe("LLM");
    expect(FISpanKind.EMBEDDING).toBe("EMBEDDING");
  });
});

describe("Mistral Instrumentation - Configuration Options", () => {
  it("should accept empty configuration", () => {
    const inst = new MistralInstrumentation({});
    expect(inst).toBeInstanceOf(MistralInstrumentation);
  });

  it("should accept only instrumentationConfig", () => {
    const inst = new MistralInstrumentation({
      instrumentationConfig: { enabled: true },
    });
    expect(inst).toBeInstanceOf(MistralInstrumentation);
  });

  it("should accept only traceConfig", () => {
    const inst = new MistralInstrumentation({
      traceConfig: { hideInputs: false },
    });
    expect(inst).toBeInstanceOf(MistralInstrumentation);
  });

  it("should accept both configs", () => {
    const inst = new MistralInstrumentation({
      instrumentationConfig: { enabled: true },
      traceConfig: { hideInputs: false, hideOutputs: false },
    });
    expect(inst).toBeInstanceOf(MistralInstrumentation);
  });
});

describe("Mistral Instrumentation - Supported Models", () => {
  const supportedModels = [
    "mistral-large-latest",
    "mistral-medium-latest",
    "mistral-small-latest",
    "open-mistral-7b",
    "open-mixtral-8x7b",
    "open-mixtral-8x22b",
    "codestral-latest",
    "mistral-embed",
  ];

  supportedModels.forEach((model) => {
    it(`should be able to configure instrumentation for model: ${model}`, () => {
      const inst = new MistralInstrumentation();
      expect(inst).toBeInstanceOf(MistralInstrumentation);
    });
  });
});

describe("Mistral Instrumentation - Version", () => {
  it("should export version", () => {
    const { VERSION } = require("../version");
    expect(typeof VERSION).toBe("string");
    expect(VERSION).toBe("0.1.0");
  });
});

describe("Mistral Instrumentation - Type Utils", () => {
  it("should export isString utility", () => {
    const { isString } = require("../typeUtils");
    expect(typeof isString).toBe("function");
    expect(isString("test")).toBe(true);
    expect(isString(123)).toBe(false);
    expect(isString(null)).toBe(false);
    expect(isString(undefined)).toBe(false);
  });

  it("should export assertUnreachable utility", () => {
    const { assertUnreachable } = require("../typeUtils");
    expect(typeof assertUnreachable).toBe("function");
    expect(() => assertUnreachable("test" as never)).toThrow("Unreachable");
  });
});
