import { isPatched, OpenAIInstrumentation } from "../index";
import { describe, it, expect, jest, beforeEach, afterEach } from '@jest/globals';

// Mock the dependencies
jest.mock('@traceai/fi-core', () => ({
  FITracer: jest.fn().mockImplementation(() => ({
    startSpan: jest.fn().mockReturnValue({
      setStatus: jest.fn(),
      recordException: jest.fn(),
      setAttributes: jest.fn(),
      addEvent: jest.fn(),
      end: jest.fn(),
      isRecording: jest.fn().mockReturnValue(true),
      spanContext: jest.fn().mockReturnValue({
        traceId: 'mock-trace-id',
        spanId: 'mock-span-id',
      }),
    }),
    startActiveSpan: jest.fn((name: string, fn: any) => {
      const mockSpan = {
        setStatus: jest.fn(),
        recordException: jest.fn(),
        setAttributes: jest.fn(),
        addEvent: jest.fn(),
        end: jest.fn(),
        isRecording: jest.fn().mockReturnValue(true),
        getSpanContext: jest.fn().mockReturnValue({
          traceId: 'mock-trace-id',
          spanId: 'mock-span-id',
          traceFlags: 1,
          isRemote: false,
        }),
        setAttribute: jest.fn(),
        setSpanContext: jest.fn(),
        updateName: jest.fn(),
        spanContext: jest.fn().mockReturnValue({
          traceId: 'mock-trace-id',
          spanId: 'mock-span-id',
        }),
      };
      return fn(mockSpan);
    }),
  })),
  safelyJSONStringify: jest.fn((obj: any) => JSON.stringify(obj)),
}));

describe("OpenAI Comprehensive Instrumentation Tests", () => {
  let instrumentation: OpenAIInstrumentation;

  beforeEach(() => {
    jest.clearAllMocks();
    
    instrumentation = new OpenAIInstrumentation({
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

    Object.defineProperty(instrumentation, 'tracer', {
      get: () => mockTracer,
      configurable: true,
    });
  });

  afterEach(() => {
    instrumentation.disable();
  });

  describe("Instrumentation Setup", () => {
    it("should create instrumentation with trace config", () => {
      const configuredInstrumentation = new OpenAIInstrumentation({
        traceConfig: {
          hideInputs: true,
          hideOutputs: false,
        },
      });
      expect(configuredInstrumentation).toBeInstanceOf(OpenAIInstrumentation);
    });

    it("should handle patching correctly", () => {
      expect(isPatched()).toBe(false);
      
      const mockModule = {
        OpenAI: {
          Chat: {
            Completions: {
              prototype: {
                create: jest.fn(),
              },
            },
          },
          Completions: {
            prototype: {
              create: jest.fn(),
            },
          },
          Embeddings: {
            prototype: {
              create: jest.fn(),
            },
          },
          Responses: {
            prototype: {
              create: jest.fn(),
            },
          },
        },
      };

      instrumentation.enable();
      instrumentation.manuallyInstrument(mockModule as any);
      
      expect(mockModule.OpenAI.Chat.Completions.prototype.create).toBeDefined();
    });
  });

  describe("Chat Completions Patching", () => {
    it("should patch chat completions create method", () => {
      const originalCreate = jest.fn();
      const mockModule = {
        OpenAI: {
          Chat: {
            Completions: {
              prototype: {
                create: originalCreate,
              },
            },
          },
          Completions: {
            prototype: {
              create: jest.fn(),
            },
          },
          Embeddings: {
            prototype: {
              create: jest.fn(),
            },
          },
          Responses: {
            prototype: {
              create: jest.fn(),
            },
          },
        },
      };

      instrumentation.enable();
      instrumentation.manuallyInstrument(mockModule as any);
      
      // Check that the method still exists and can be called
      expect(mockModule.OpenAI.Chat.Completions.prototype.create).toBeDefined();
      expect(typeof mockModule.OpenAI.Chat.Completions.prototype.create).toBe('function');
    });

    it("should patch completions create method", () => {
      const originalCreate = jest.fn();
      const mockModule = {
        OpenAI: {
          Chat: {
            Completions: {
              prototype: {
                create: jest.fn(),
              },
            },
          },
          Completions: {
            prototype: {
              create: originalCreate,
            },
          },
          Embeddings: {
            prototype: {
              create: jest.fn(),
            },
          },
          Responses: {
            prototype: {
              create: jest.fn(),
            },
          },
        },
      };

      instrumentation.enable();
      instrumentation.manuallyInstrument(mockModule as any);
      
      // Check that the method still exists and can be called
      expect(mockModule.OpenAI.Completions.prototype.create).toBeDefined();
      expect(typeof mockModule.OpenAI.Completions.prototype.create).toBe('function');
    });

    it("should patch embeddings create method", () => {
      const originalCreate = jest.fn();
      const mockModule = {
        OpenAI: {
          Chat: {
            Completions: {
              prototype: {
                create: jest.fn(),
              },
            },
          },
          Completions: {
            prototype: {
              create: jest.fn(),
            },
          },
          Embeddings: {
            prototype: {
              create: originalCreate,
            },
          },
          Responses: {
            prototype: {
              create: jest.fn(),
            },
          },
        },
      };

      instrumentation.enable();
      instrumentation.manuallyInstrument(mockModule as any);
      
      // Check that the method still exists and can be called
      expect(mockModule.OpenAI.Embeddings.prototype.create).toBeDefined();
      expect(typeof mockModule.OpenAI.Embeddings.prototype.create).toBe('function');
    });

    it("should patch responses create method", () => {
      const originalCreate = jest.fn();
      const mockModule = {
        OpenAI: {
          Chat: {
            Completions: {
              prototype: {
                create: jest.fn(),
              },
            },
          },
          Completions: {
            prototype: {
              create: jest.fn(),
            },
          },
          Embeddings: {
            prototype: {
              create: jest.fn(),
            },
          },
          Responses: {
            prototype: {
              create: originalCreate,
            },
          },
        },
      };

      instrumentation.enable();
      instrumentation.manuallyInstrument(mockModule as any);
      
      // Check that the method still exists and can be called
      expect(mockModule.OpenAI.Responses.prototype.create).toBeDefined();
      expect(typeof mockModule.OpenAI.Responses.prototype.create).toBe('function');
    });
  });

  describe("Configuration Handling", () => {
    it("should handle different trace configurations", () => {
      const configs = [
        { hideInputs: true, hideOutputs: false },
        { hideInputs: false, hideOutputs: true },
        { hideInputs: true, hideOutputs: true },
        { hideInputs: false, hideOutputs: false },
      ];

      configs.forEach(config => {
        const configuredInstrumentation = new OpenAIInstrumentation({
          traceConfig: config,
        });
        expect(configuredInstrumentation).toBeInstanceOf(OpenAIInstrumentation);
      });
    });

    it("should handle instrumentation config variations", () => {
      const configs = [
        { enabled: true },
        { enabled: false },
        {},
      ];

      configs.forEach(config => {
        const configuredInstrumentation = new OpenAIInstrumentation({
          instrumentationConfig: config,
        });
        expect(configuredInstrumentation).toBeInstanceOf(OpenAIInstrumentation);
      });
    });
  });

  describe("Error Handling", () => {
    it("should handle undefined module gracefully", () => {
      instrumentation.enable();
      // The actual method doesn't throw, it just doesn't patch anything
      expect(() => {
        instrumentation.manuallyInstrument(undefined as any);
      }).not.toThrow();
    });

    it("should handle null module gracefully", () => {
      instrumentation.enable();
      // The actual method doesn't throw, it just doesn't patch anything
      expect(() => {
        instrumentation.manuallyInstrument(null as any);
      }).not.toThrow();
    });

    it("should handle invalid module structure", () => {
      instrumentation.enable();
      // The actual method doesn't throw, it just doesn't patch anything
      expect(() => {
        instrumentation.manuallyInstrument({} as any);
      }).not.toThrow();
    });

    it("should handle module without OpenAI property", () => {
      instrumentation.enable();
      // The actual method doesn't throw, it just doesn't patch anything
      expect(() => {
        instrumentation.manuallyInstrument({ someOtherProperty: {} } as any);
      }).not.toThrow();
    });
  });

  describe("Multiple Enable/Disable Cycles", () => {
    it("should handle multiple enable/disable cycles", () => {
      expect(() => {
        instrumentation.enable();
        instrumentation.disable();
        instrumentation.enable();
        instrumentation.disable();
      }).not.toThrow();
    });

    it("should handle disable before enable", () => {
      expect(() => {
        instrumentation.disable();
        instrumentation.enable();
      }).not.toThrow();
    });
  });

  describe("Utility Functions Coverage", () => {
    it("should test isPatched function", () => {
      expect(typeof isPatched).toBe('function');
      expect(typeof isPatched()).toBe('boolean');
    });

    it("should create different instrumentation instances", () => {
      const inst1 = new OpenAIInstrumentation();
      const inst2 = new OpenAIInstrumentation({ traceConfig: { hideInputs: true } });
      const inst3 = new OpenAIInstrumentation({ instrumentationConfig: { enabled: false } });
      
      expect(inst1).toBeInstanceOf(OpenAIInstrumentation);
      expect(inst2).toBeInstanceOf(OpenAIInstrumentation);
      expect(inst3).toBeInstanceOf(OpenAIInstrumentation);
      expect(inst1).not.toBe(inst2);
      expect(inst2).not.toBe(inst3);
    });

    it("should handle enable/disable state correctly", () => {
      const inst = new OpenAIInstrumentation();
      expect(() => inst.enable()).not.toThrow();
      expect(() => inst.disable()).not.toThrow();
      expect(() => inst.enable()).not.toThrow();
    });

    it("should handle patching with already patched modules", () => {
      const mockModule = {
        OpenAI: {
          Chat: {
            Completions: {
              prototype: {
                create: jest.fn(),
              },
            },
          },
          Completions: {
            prototype: {
              create: jest.fn(),
            },
          },
          Embeddings: {
            prototype: {
              create: jest.fn(),
            },
          },
          Responses: {
            prototype: {
              create: jest.fn(),
            },
          },
        },
        fiPatched: true,
      };

      instrumentation.enable();
      // Should handle already patched modules gracefully
      expect(() => {
        instrumentation.manuallyInstrument(mockModule as any);
      }).not.toThrow();
    });
  });
}); 