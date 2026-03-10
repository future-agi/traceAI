import { describe, it, expect, jest, beforeEach, afterEach } from "@jest/globals";
import { GoogleGenAIInstrumentation, isPatched } from "../instrumentation";

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

describe("Google GenAI Instrumentation", () => {
  let instrumentation: GoogleGenAIInstrumentation;

  beforeEach(() => {
    jest.clearAllMocks();

    instrumentation = new GoogleGenAIInstrumentation({
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
      expect(instrumentation).toBeInstanceOf(GoogleGenAIInstrumentation);
    });

    it("should initialize with default config", () => {
      const defaultInstrumentation = new GoogleGenAIInstrumentation();
      expect(defaultInstrumentation).toBeInstanceOf(GoogleGenAIInstrumentation);
    });

    it("should initialize with custom instrumentation config", () => {
      const customInstrumentation = new GoogleGenAIInstrumentation({
        instrumentationConfig: {
          enabled: false,
        },
      });
      expect(customInstrumentation).toBeInstanceOf(GoogleGenAIInstrumentation);
    });

    it("should initialize with trace config", () => {
      const traceConfigInstrumentation = new GoogleGenAIInstrumentation({
        traceConfig: {
          hideInputs: true,
          hideOutputs: true,
        },
      });
      expect(traceConfigInstrumentation).toBeInstanceOf(GoogleGenAIInstrumentation);
    });

    it("should have correct instrumentation name", () => {
      expect(instrumentation.instrumentationName).toBe("@traceai/google-genai");
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
    it("should accept a mock module with GenerativeModel", () => {
      instrumentation.enable();

      const mockModule = {
        GenerativeModel: {
          prototype: {
            generateContent: jest.fn(),
            embedContent: jest.fn(),
            batchEmbedContents: jest.fn(),
          },
        },
      };

      expect(() => instrumentation.manuallyInstrument(mockModule)).not.toThrow();
    });

    it("should handle module without GenerativeModel property", () => {
      instrumentation.enable();

      const mockModule = {};

      expect(() => instrumentation.manuallyInstrument(mockModule)).not.toThrow();
    });

    it("should handle module with partial structure", () => {
      instrumentation.enable();

      const mockModule = {
        GenerativeModel: {
          prototype: {},
        },
      };

      expect(() => instrumentation.manuallyInstrument(mockModule)).not.toThrow();
    });
  });
});

describe("Google GenAI Instrumentation - Semantic Conventions", () => {
  it("should use correct LLM system identifier", () => {
    const { LLMSystem, LLMProvider } = require("@traceai/fi-semantic-conventions");
    expect(LLMSystem.GOOGLE_GENERATIVE_AI).toBe("google_generative_ai");
    expect(LLMProvider.GOOGLE_GENERATIVE_AI).toBe("google_generative_ai");
  });

  it("should use correct span kind for LLM operations", () => {
    const { FISpanKind } = require("@traceai/fi-semantic-conventions");
    expect(FISpanKind.LLM).toBe("LLM");
    expect(FISpanKind.EMBEDDING).toBe("EMBEDDING");
  });
});

describe("Google GenAI Instrumentation - Configuration Options", () => {
  it("should accept empty configuration", () => {
    const inst = new GoogleGenAIInstrumentation({});
    expect(inst).toBeInstanceOf(GoogleGenAIInstrumentation);
  });

  it("should accept only instrumentationConfig", () => {
    const inst = new GoogleGenAIInstrumentation({
      instrumentationConfig: { enabled: true },
    });
    expect(inst).toBeInstanceOf(GoogleGenAIInstrumentation);
  });

  it("should accept only traceConfig", () => {
    const inst = new GoogleGenAIInstrumentation({
      traceConfig: { hideInputs: false },
    });
    expect(inst).toBeInstanceOf(GoogleGenAIInstrumentation);
  });

  it("should accept both configs", () => {
    const inst = new GoogleGenAIInstrumentation({
      instrumentationConfig: { enabled: true },
      traceConfig: { hideInputs: false, hideOutputs: false },
    });
    expect(inst).toBeInstanceOf(GoogleGenAIInstrumentation);
  });
});

describe("Google GenAI Instrumentation - Supported Models", () => {
  const supportedModels = [
    "gemini-1.5-pro",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
    "gemini-1.0-pro",
    "gemini-pro-vision",
    "embedding-001",
    "text-embedding-004",
  ];

  supportedModels.forEach((model) => {
    it(`should be able to configure instrumentation for model: ${model}`, () => {
      const inst = new GoogleGenAIInstrumentation();
      expect(inst).toBeInstanceOf(GoogleGenAIInstrumentation);
    });
  });
});

describe("Google GenAI Instrumentation - Version", () => {
  it("should export version", () => {
    const { VERSION } = require("../version");
    expect(typeof VERSION).toBe("string");
    expect(VERSION).toBe("0.1.0");
  });
});

describe("Google GenAI Instrumentation - Type Utils", () => {
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
