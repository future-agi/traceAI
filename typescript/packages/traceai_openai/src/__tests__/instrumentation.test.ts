import { describe, it, expect, jest, beforeEach, afterEach } from '@jest/globals';
import { OpenAIInstrumentation, isPatched } from '../instrumentation';

// Mock the dependencies
jest.mock('@traceai/fi-core', () => ({
  FITracer: jest.fn().mockImplementation(() => ({
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
      };
      return fn(mockSpan);
    }),
  })),
  safelyJSONStringify: jest.fn((obj: any) => JSON.stringify(obj)),
}));

describe('OpenAI Instrumentation', () => {
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

  describe('Initialization', () => {
    it('should create instrumentation instance', () => {
      expect(instrumentation).toBeInstanceOf(OpenAIInstrumentation);
    });

    it('should initialize with default config', () => {
      const defaultInstrumentation = new OpenAIInstrumentation();
      expect(defaultInstrumentation).toBeInstanceOf(OpenAIInstrumentation);
    });

    it('should initialize with custom instrumentation config', () => {
      const customInstrumentation = new OpenAIInstrumentation({
        instrumentationConfig: {
          enabled: false,
        },
      });
      expect(customInstrumentation).toBeInstanceOf(OpenAIInstrumentation);
    });

    it('should initialize with trace config', () => {
      const traceConfigInstrumentation = new OpenAIInstrumentation({
        traceConfig: {
          hideInputs: false,
          hideOutputs: false,
        },
      });
      expect(traceConfigInstrumentation).toBeInstanceOf(OpenAIInstrumentation);
    });
  });

  describe('Patching Status', () => {
    it('should report not patched initially', () => {
      expect(isPatched()).toBe(false);
    });

    it('should handle undefined module gracefully', () => {
      instrumentation.enable();
      expect(() => {
        instrumentation.manuallyInstrument(undefined as any);
      }).toThrow();
    });

    it('should handle null module gracefully', () => {
      instrumentation.enable();
      expect(() => {
        instrumentation.manuallyInstrument(null as any);
      }).toThrow();
    });

    it('should handle already patched module', () => {
      const mockModule = {
        OpenAI: {
          Chat: {
            Completions: {
              prototype: {
                create: jest.fn(),
              },
            },
          },
        },
        fiPatched: true,
      };

      instrumentation.enable();
      const result = instrumentation.manuallyInstrument(mockModule as any);
      expect(result).toBeUndefined(); // manuallyInstrument is void
      expect(mockModule.fiPatched).toBe(true);
    });

    it('should patch OpenAI module', () => {
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
      const result = instrumentation.manuallyInstrument(mockModule as any);
      
      expect(result).toBeUndefined(); // manuallyInstrument is void
      expect(mockModule.OpenAI.Chat.Completions.prototype.create).toBeDefined();
    });
  });

  describe('Trace Configuration', () => {
    it('should respect input hiding configuration', () => {
      const maskedInstrumentation = new OpenAIInstrumentation({
        traceConfig: {
          hideInputs: true,
          hideOutputs: false,
        },
      });

      expect(maskedInstrumentation).toBeInstanceOf(OpenAIInstrumentation);
      expect((maskedInstrumentation as any)._traceConfig?.hideInputs).toBe(true);
    });

    it('should respect output hiding configuration', () => {
      const maskedInstrumentation = new OpenAIInstrumentation({
        traceConfig: {
          hideInputs: false,
          hideOutputs: true,
        },
      });

      expect(maskedInstrumentation).toBeInstanceOf(OpenAIInstrumentation);
      expect((maskedInstrumentation as any)._traceConfig?.hideOutputs).toBe(true);
    });

    it('should handle complex trace configuration', () => {
      const complexInstrumentation = new OpenAIInstrumentation({
        traceConfig: {
          hideInputs: true,
          hideOutputs: true,
          hideInputMessages: true,
          hideOutputMessages: true,
          hideEmbeddingVectors: true,
          base64ImageMaxLength: 1000,
        },
      });

      expect(complexInstrumentation).toBeInstanceOf(OpenAIInstrumentation);
      expect((complexInstrumentation as any)._traceConfig?.hideInputs).toBe(true);
      expect((complexInstrumentation as any)._traceConfig?.hideOutputs).toBe(true);
      expect((complexInstrumentation as any)._traceConfig?.base64ImageMaxLength).toBe(1000);
    });

    it('should handle minimal configuration', () => {
      const minimalInstrumentation = new OpenAIInstrumentation({
        traceConfig: {},
      });

      expect(minimalInstrumentation).toBeInstanceOf(OpenAIInstrumentation);
    });
  });

  describe('Module Lifecycle', () => {
    it('should handle enable and disable', () => {
      expect(() => {
        instrumentation.enable();
        instrumentation.disable();
      }).not.toThrow();
    });

    it('should handle multiple enables', () => {
      expect(() => {
        instrumentation.enable();
        instrumentation.enable(); // Should not throw
        instrumentation.disable();
      }).not.toThrow();
    });

    it('should handle disable without enable', () => {
      const newInstrumentation = new OpenAIInstrumentation();
      expect(() => {
        newInstrumentation.disable(); // Should not throw
      }).not.toThrow();
    });

    it('should handle enable after disable', () => {
      expect(() => {
        instrumentation.enable();
        instrumentation.disable();
        instrumentation.enable(); // Should work
        instrumentation.disable();
      }).not.toThrow();
    });
  });

  describe('Error Handling', () => {
    it('should handle malformed modules gracefully', () => {
      const malformedModule = {
        OpenAI: null,
      };

      instrumentation.enable();
      expect(() => {
        instrumentation.manuallyInstrument(malformedModule as any);
      }).not.toThrow();
    });

    it('should handle modules without prototypes', () => {
      const moduleWithoutPrototypes = {
        OpenAI: {
          Chat: {
            Completions: {},
          },
        },
      };

      instrumentation.enable();
      expect(() => {
        instrumentation.manuallyInstrument(moduleWithoutPrototypes as any);
      }).not.toThrow();
    });

    it('should handle empty modules', () => {
      const emptyModule = {};

      instrumentation.enable();
      expect(() => {
        instrumentation.manuallyInstrument(emptyModule as any);
      }).not.toThrow();
    });
  });

  describe('Instrumentation Features', () => {
    it('should have correct module name and version', () => {
      expect(instrumentation.instrumentationName).toBe('@traceai/fi-instrumentation-openai');
      expect(instrumentation.instrumentationVersion).toBeDefined();
    });

    it('should support manual instrumentation', () => {
      const mockModule = {
        OpenAI: {
          Chat: {
            Completions: {
              prototype: {
                create: jest.fn(),
              },
            },
          },
        },
      };

      instrumentation.enable();
      expect(() => {
        instrumentation.manuallyInstrument(mockModule as any);
      }).not.toThrow();
    });

    it('should handle instrumentation when disabled', () => {
      const mockModule = {
        OpenAI: {
          Chat: {
            Completions: {
              prototype: {
                create: jest.fn(),
              },
            },
          },
        },
      };

      // Don't enable instrumentation
      const result = instrumentation.manuallyInstrument(mockModule as any);
      expect(result).toBeUndefined(); // manuallyInstrument is void
    });
  });

  describe('FITracer Integration', () => {
    it('should initialize FITracer on enable', () => {
      const { FITracer } = require('@traceai/fi-core');
      
      instrumentation.enable();
      
      expect(FITracer).toHaveBeenCalledWith({
        tracer: expect.any(Object),
        traceConfig: undefined,
      });
    });

    it('should initialize FITracer with trace config', () => {
      const { FITracer } = require('@traceai/fi-core');
      
      const instrumentationWithConfig = new OpenAIInstrumentation({
        traceConfig: {
          hideInputs: true,
        },
      });

      instrumentationWithConfig.enable();
      
      expect(FITracer).toHaveBeenCalledWith({
        tracer: expect.any(Object),
        traceConfig: { hideInputs: true },
      });

      instrumentationWithConfig.disable();
    });
  });

  describe('OpenAI Version Support', () => {
    it('should support OpenAI v4', () => {
      const mockModuleV4 = {
        version: '4.52.7',
        OpenAI: {
          Chat: {
            Completions: {
              prototype: {
                create: jest.fn(),
              },
            },
          },
        },
      };

      instrumentation.enable();
      expect(() => {
        instrumentation.manuallyInstrument(mockModuleV4 as any);
      }).not.toThrow();
    });

    it('should support OpenAI v5', () => {
      const mockModuleV5 = {
        version: '5.0.0',
        OpenAI: {
          Chat: {
            Completions: {
              prototype: {
                create: jest.fn(),
              },
            },
          },
        },
      };

      instrumentation.enable();
      expect(() => {
        instrumentation.manuallyInstrument(mockModuleV5 as any);
      }).not.toThrow();
    });
  });
}); 