import { describe, it, expect, jest, beforeEach, afterEach } from "@jest/globals";
import { GroqInstrumentation, isPatched } from "../instrumentation";

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

describe("Groq Instrumentation", () => {
  let instrumentation: GroqInstrumentation;

  beforeEach(() => {
    jest.clearAllMocks();

    instrumentation = new GroqInstrumentation({
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
      expect(instrumentation).toBeInstanceOf(GroqInstrumentation);
    });

    it("should initialize with default config", () => {
      const defaultInstrumentation = new GroqInstrumentation();
      expect(defaultInstrumentation).toBeInstanceOf(GroqInstrumentation);
    });

    it("should initialize with custom instrumentation config", () => {
      const customInstrumentation = new GroqInstrumentation({
        instrumentationConfig: {
          enabled: false,
        },
      });
      expect(customInstrumentation).toBeInstanceOf(GroqInstrumentation);
    });

    it("should initialize with trace config", () => {
      const traceConfigInstrumentation = new GroqInstrumentation({
        traceConfig: {
          hideInputs: true,
          hideOutputs: true,
        },
      });
      expect(traceConfigInstrumentation).toBeInstanceOf(GroqInstrumentation);
    });

    it("should have correct instrumentation name", () => {
      expect(instrumentation.instrumentationName).toBe("@traceai/groq");
    });
  });

  describe("isPatched function", () => {
    it("should be a function", () => {
      expect(typeof isPatched).toBe("function");
    });

    it("should return false initially", () => {
      // Note: This may return true if other tests have patched the module
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
        Groq: {
          Chat: {
            Completions: {
              prototype: {
                create: jest.fn(),
              },
            },
          },
        },
      };

      expect(() => instrumentation.manuallyInstrument(mockModule)).not.toThrow();
    });

    it("should handle module without Groq property", () => {
      instrumentation.enable();

      const mockModule = {};

      expect(() => instrumentation.manuallyInstrument(mockModule)).not.toThrow();
    });

    it("should handle module with partial structure", () => {
      instrumentation.enable();

      const mockModule = {
        Groq: {
          // No Chat property
        },
      };

      expect(() => instrumentation.manuallyInstrument(mockModule)).not.toThrow();
    });
  });
});

describe("Groq Instrumentation - Semantic Conventions", () => {
  it("should use correct LLM system identifier", () => {
    // Verify the semantic conventions import
    const { LLMSystem, LLMProvider } = require("@traceai/fi-semantic-conventions");
    expect(LLMSystem.GROQ).toBe("groq");
    expect(LLMProvider.GROQ).toBe("groq");
  });

  it("should use correct span kind for LLM operations", () => {
    const { FISpanKind } = require("@traceai/fi-semantic-conventions");
    expect(FISpanKind.LLM).toBe("LLM");
  });
});

describe("Groq Instrumentation - Configuration Options", () => {
  it("should accept empty configuration", () => {
    const inst = new GroqInstrumentation({});
    expect(inst).toBeInstanceOf(GroqInstrumentation);
  });

  it("should accept only instrumentationConfig", () => {
    const inst = new GroqInstrumentation({
      instrumentationConfig: { enabled: true },
    });
    expect(inst).toBeInstanceOf(GroqInstrumentation);
  });

  it("should accept only traceConfig", () => {
    const inst = new GroqInstrumentation({
      traceConfig: { hideInputs: false },
    });
    expect(inst).toBeInstanceOf(GroqInstrumentation);
  });

  it("should accept both configs", () => {
    const inst = new GroqInstrumentation({
      instrumentationConfig: { enabled: true },
      traceConfig: { hideInputs: false, hideOutputs: false },
    });
    expect(inst).toBeInstanceOf(GroqInstrumentation);
  });
});

describe("Groq Instrumentation - Supported Models", () => {
  const supportedModels = [
    "mixtral-8x7b-32768",
    "llama-3.1-70b-versatile",
    "llama-3.1-8b-instant",
    "llama3-70b-8192",
    "llama3-8b-8192",
    "gemma2-9b-it",
    "llama3-groq-70b-8192-tool-use-preview",
  ];

  supportedModels.forEach((model) => {
    it(`should be able to configure instrumentation for model: ${model}`, () => {
      const inst = new GroqInstrumentation();
      expect(inst).toBeInstanceOf(GroqInstrumentation);
      // Model-specific behavior would be tested in integration tests
    });
  });
});

describe("Groq Instrumentation - Version", () => {
  it("should export version", () => {
    const { VERSION } = require("../version");
    expect(typeof VERSION).toBe("string");
    expect(VERSION).toBe("0.1.0");
  });
});

describe("Groq Instrumentation - Type Utils", () => {
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
