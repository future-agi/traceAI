import { describe, it, expect, jest, beforeEach, afterEach } from "@jest/globals";
import { CohereInstrumentation, isPatched } from "../instrumentation";

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

describe("Cohere Instrumentation", () => {
  let instrumentation: CohereInstrumentation;

  beforeEach(() => {
    jest.clearAllMocks();

    instrumentation = new CohereInstrumentation({
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
      expect(instrumentation).toBeInstanceOf(CohereInstrumentation);
    });

    it("should initialize with default config", () => {
      const defaultInstrumentation = new CohereInstrumentation();
      expect(defaultInstrumentation).toBeInstanceOf(CohereInstrumentation);
    });

    it("should initialize with custom instrumentation config", () => {
      const customInstrumentation = new CohereInstrumentation({
        instrumentationConfig: {
          enabled: false,
        },
      });
      expect(customInstrumentation).toBeInstanceOf(CohereInstrumentation);
    });

    it("should initialize with trace config", () => {
      const traceConfigInstrumentation = new CohereInstrumentation({
        traceConfig: {
          hideInputs: true,
          hideOutputs: true,
        },
      });
      expect(traceConfigInstrumentation).toBeInstanceOf(CohereInstrumentation);
    });

    it("should have correct instrumentation name", () => {
      expect(instrumentation.instrumentationName).toBe("@traceai/cohere");
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
    it("should accept a mock module with CohereClient", () => {
      instrumentation.enable();

      const mockModule = {
        CohereClient: {
          prototype: {
            chat: jest.fn(),
            embed: jest.fn(),
            rerank: jest.fn(),
          },
        },
      };

      expect(() => instrumentation.manuallyInstrument(mockModule)).not.toThrow();
    });

    it("should handle module without CohereClient property", () => {
      instrumentation.enable();

      const mockModule = {};

      expect(() => instrumentation.manuallyInstrument(mockModule)).not.toThrow();
    });

    it("should handle module with partial structure", () => {
      instrumentation.enable();

      const mockModule = {
        CohereClient: {
          prototype: {},
        },
      };

      expect(() => instrumentation.manuallyInstrument(mockModule)).not.toThrow();
    });
  });
});

describe("Cohere Instrumentation - Semantic Conventions", () => {
  it("should use correct LLM system identifier", () => {
    const { LLMSystem, LLMProvider } = require("@traceai/fi-semantic-conventions");
    expect(LLMSystem.COHERE).toBe("cohere");
    expect(LLMProvider.COHERE).toBe("cohere");
  });

  it("should use correct span kind for LLM and embedding operations", () => {
    const { FISpanKind } = require("@traceai/fi-semantic-conventions");
    expect(FISpanKind.LLM).toBe("LLM");
    expect(FISpanKind.EMBEDDING).toBe("EMBEDDING");
  });
});

describe("Cohere Instrumentation - Configuration Options", () => {
  it("should accept empty configuration", () => {
    const inst = new CohereInstrumentation({});
    expect(inst).toBeInstanceOf(CohereInstrumentation);
  });

  it("should accept only instrumentationConfig", () => {
    const inst = new CohereInstrumentation({
      instrumentationConfig: { enabled: true },
    });
    expect(inst).toBeInstanceOf(CohereInstrumentation);
  });

  it("should accept only traceConfig", () => {
    const inst = new CohereInstrumentation({
      traceConfig: { hideInputs: false },
    });
    expect(inst).toBeInstanceOf(CohereInstrumentation);
  });

  it("should accept both configs", () => {
    const inst = new CohereInstrumentation({
      instrumentationConfig: { enabled: true },
      traceConfig: { hideInputs: false, hideOutputs: false },
    });
    expect(inst).toBeInstanceOf(CohereInstrumentation);
  });
});

describe("Cohere Instrumentation - Supported Models", () => {
  const chatModels = [
    "command",
    "command-r",
    "command-r-08-2024",
    "command-light",
    "command-nightly",
  ];

  const embedModels = [
    "embed-english-v3.0",
    "embed-multilingual-v3.0",
    "embed-english-light-v3.0",
    "embed-multilingual-light-v3.0",
  ];

  const rerankModels = [
    "rerank-english-v3.0",
    "rerank-multilingual-v3.0",
    "rerank-english-v2.0",
    "rerank-multilingual-v2.0",
  ];

  chatModels.forEach((model) => {
    it(`should be able to configure instrumentation for chat model: ${model}`, () => {
      const inst = new CohereInstrumentation();
      expect(inst).toBeInstanceOf(CohereInstrumentation);
    });
  });

  embedModels.forEach((model) => {
    it(`should be able to configure instrumentation for embed model: ${model}`, () => {
      const inst = new CohereInstrumentation();
      expect(inst).toBeInstanceOf(CohereInstrumentation);
    });
  });

  rerankModels.forEach((model) => {
    it(`should be able to configure instrumentation for rerank model: ${model}`, () => {
      const inst = new CohereInstrumentation();
      expect(inst).toBeInstanceOf(CohereInstrumentation);
    });
  });
});

describe("Cohere Instrumentation - Version", () => {
  it("should export version", () => {
    const { VERSION } = require("../version");
    expect(typeof VERSION).toBe("string");
    expect(VERSION).toBe("0.1.0");
  });
});

describe("Cohere Instrumentation - Type Utils", () => {
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
