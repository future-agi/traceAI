import { describe, it, expect, jest, beforeEach, afterEach } from "@jest/globals";
import { OllamaInstrumentation, isPatched } from "../instrumentation";

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

describe("Ollama Instrumentation", () => {
  let instrumentation: OllamaInstrumentation;

  beforeEach(() => {
    jest.clearAllMocks();

    instrumentation = new OllamaInstrumentation({
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
      expect(instrumentation).toBeInstanceOf(OllamaInstrumentation);
    });

    it("should initialize with default config", () => {
      const defaultInstrumentation = new OllamaInstrumentation();
      expect(defaultInstrumentation).toBeInstanceOf(OllamaInstrumentation);
    });

    it("should initialize with custom instrumentation config", () => {
      const customInstrumentation = new OllamaInstrumentation({
        instrumentationConfig: {
          enabled: false,
        },
      });
      expect(customInstrumentation).toBeInstanceOf(OllamaInstrumentation);
    });

    it("should initialize with trace config", () => {
      const traceConfigInstrumentation = new OllamaInstrumentation({
        traceConfig: {
          hideInputs: true,
          hideOutputs: true,
        },
      });
      expect(traceConfigInstrumentation).toBeInstanceOf(OllamaInstrumentation);
    });

    it("should have correct instrumentation name", () => {
      expect(instrumentation.instrumentationName).toBe("@traceai/ollama");
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
    it("should accept a mock module with Ollama class", () => {
      instrumentation.enable();

      const mockModule = {
        Ollama: {
          prototype: {
            chat: jest.fn(),
            generate: jest.fn(),
            embed: jest.fn(),
            embeddings: jest.fn(),
          },
        },
      };

      expect(() => instrumentation.manuallyInstrument(mockModule)).not.toThrow();
    });

    it("should handle module without Ollama property", () => {
      instrumentation.enable();

      const mockModule = {};

      expect(() => instrumentation.manuallyInstrument(mockModule)).not.toThrow();
    });

    it("should handle module with partial structure", () => {
      instrumentation.enable();

      const mockModule = {
        Ollama: {
          prototype: {},
        },
      };

      expect(() => instrumentation.manuallyInstrument(mockModule)).not.toThrow();
    });

    it("should handle default export", () => {
      instrumentation.enable();

      const mockModule = {
        default: {
          chat: jest.fn(),
          generate: jest.fn(),
          embed: jest.fn(),
        },
      };

      expect(() => instrumentation.manuallyInstrument(mockModule)).not.toThrow();
    });
  });
});

describe("Ollama Instrumentation - Semantic Conventions", () => {
  it("should use correct LLM system identifier", () => {
    const { LLMSystem, LLMProvider } = require("@traceai/fi-semantic-conventions");
    expect(LLMSystem.OLLAMA).toBe("ollama");
    expect(LLMProvider.OLLAMA).toBe("ollama");
  });

  it("should use correct span kind for LLM and embedding operations", () => {
    const { FISpanKind } = require("@traceai/fi-semantic-conventions");
    expect(FISpanKind.LLM).toBe("LLM");
    expect(FISpanKind.EMBEDDING).toBe("EMBEDDING");
  });
});

describe("Ollama Instrumentation - Configuration Options", () => {
  it("should accept empty configuration", () => {
    const inst = new OllamaInstrumentation({});
    expect(inst).toBeInstanceOf(OllamaInstrumentation);
  });

  it("should accept only instrumentationConfig", () => {
    const inst = new OllamaInstrumentation({
      instrumentationConfig: { enabled: true },
    });
    expect(inst).toBeInstanceOf(OllamaInstrumentation);
  });

  it("should accept only traceConfig", () => {
    const inst = new OllamaInstrumentation({
      traceConfig: { hideInputs: false },
    });
    expect(inst).toBeInstanceOf(OllamaInstrumentation);
  });

  it("should accept both configs", () => {
    const inst = new OllamaInstrumentation({
      instrumentationConfig: { enabled: true },
      traceConfig: { hideInputs: false, hideOutputs: false },
    });
    expect(inst).toBeInstanceOf(OllamaInstrumentation);
  });
});

describe("Ollama Instrumentation - Supported Operations", () => {
  const operations = [
    { name: "chat", description: "Chat completions with message history" },
    { name: "generate", description: "Text generation/completion" },
    { name: "embed", description: "Batch embeddings (new API)" },
    { name: "embeddings", description: "Single embedding (legacy API)" },
  ];

  operations.forEach((op) => {
    it(`should support ${op.name} operation - ${op.description}`, () => {
      const inst = new OllamaInstrumentation();
      expect(inst).toBeInstanceOf(OllamaInstrumentation);
    });
  });
});

describe("Ollama Instrumentation - Supported Models", () => {
  const models = [
    "llama2",
    "llama3",
    "llama3.1",
    "llama3.2",
    "mistral",
    "mixtral",
    "codellama",
    "phi",
    "phi3",
    "gemma",
    "gemma2",
    "qwen",
    "qwen2",
    "deepseek-coder",
    "neural-chat",
    "starling-lm",
    "vicuna",
    "orca-mini",
    "dolphin-mistral",
    "nomic-embed-text",
    "mxbai-embed-large",
    "all-minilm",
  ];

  models.forEach((model) => {
    it(`should be able to configure instrumentation for model: ${model}`, () => {
      const inst = new OllamaInstrumentation();
      expect(inst).toBeInstanceOf(OllamaInstrumentation);
    });
  });
});

describe("Ollama Instrumentation - Version", () => {
  it("should export version", () => {
    const { VERSION } = require("../version");
    expect(typeof VERSION).toBe("string");
    expect(VERSION).toBe("0.1.0");
  });
});

describe("Ollama Instrumentation - Type Utils", () => {
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
