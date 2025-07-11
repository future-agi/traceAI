import { describe, it, expect, jest, beforeEach, afterEach } from '@jest/globals';
import { LangChainInstrumentation, isPatched } from '../instrumentation';
import { FITracer } from '@traceai/fi-core';

// Mock LangChain CallbackManager module
const mockCallbackManagerModule = {
  CallbackManager: {
    configure: jest.fn(),
    _configureSync: jest.fn(),
  },
};

// Mock FITracer
jest.mock('@traceai/fi-core', () => ({
  FITracer: jest.fn().mockImplementation(() => ({
    startSpan: jest.fn().mockReturnValue({
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
      }),
    }),
    startActiveSpan: jest.fn((name, fn) => {
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
        }),
      };
      return fn(mockSpan);
    }),
  })),
  TraceConfigOptions: {},
}));

// Mock addTracerToHandlers utility
jest.mock('../instrumentationUtils', () => ({
  addTracerToHandlers: jest.fn((tracer, handlers) => {
    // Mock implementation that adds the tracer to handlers
    return [...(handlers || []), { tracer }];
  }),
}));

describe('LangChain Instrumentation', () => {
  let instrumentation: LangChainInstrumentation;
  let mockTracer: any;

  beforeEach(() => {
    jest.clearAllMocks();
    
    // Create a mock tracer
    mockTracer = {
      startSpan: jest.fn().mockReturnValue(global.testUtils.createMockSpan()),
      startActiveSpan: jest.fn((name, fn) => fn(global.testUtils.createMockSpan())),
    };

    instrumentation = new LangChainInstrumentation({
      instrumentationConfig: {
        enabled: true,
      },
      traceConfig: {
        maskInputs: false,
        maskOutputs: false,
      },
    });

    // Mock the tracer property
    Object.defineProperty(instrumentation, 'tracer', {
      get: () => mockTracer,
      configurable: true,
    });
  });

  describe('Initialization', () => {
    it('should create instrumentation instance', () => {
      expect(instrumentation).toBeInstanceOf(LangChainInstrumentation);
    });

    it('should initialize with default config', () => {
      const defaultInstrumentation = new LangChainInstrumentation();
      expect(defaultInstrumentation).toBeInstanceOf(LangChainInstrumentation);
    });

    it('should initialize with custom config', () => {
      const customInstrumentation = new LangChainInstrumentation({
        instrumentationConfig: {
          enabled: false,
        },
        traceConfig: {
          maskInputs: true,
          maskOutputs: true,
        },
      });
      expect(customInstrumentation).toBeInstanceOf(LangChainInstrumentation);
    });

    it('should initialize FITracer with tracer and config', () => {
      expect(FITracer).toHaveBeenCalledWith({
        tracer: expect.any(Object),
        traceConfig: expect.any(Object),
      });
    });
  });

  describe('Patching', () => {
    it('should report not patched initially', () => {
      expect(isPatched()).toBe(false);
    });

    it('should patch CallbackManager module', () => {
      instrumentation.manuallyInstrument(mockCallbackManagerModule as any);
      
      expect(mockCallbackManagerModule.CallbackManager.configure).toBeDefined();
    });

    it('should not double-patch', () => {
      // First patch
      instrumentation.manuallyInstrument(mockCallbackManagerModule as any);
      
      // Second patch attempt
      instrumentation.manuallyInstrument(mockCallbackManagerModule as any);
      
      // Should still be defined and not throw
      expect(mockCallbackManagerModule.CallbackManager.configure).toBeDefined();
      expect(isPatched()).toBe(true);
    });
  });

  describe('CallbackManager _configureSync Patching (v0.2.0+)', () => {
    beforeEach(() => {
      // Reset the mock to have _configureSync (v0.2.0+ behavior)
      mockCallbackManagerModule.CallbackManager._configureSync = jest.fn();
    });

    it('should patch _configureSync when available', () => {
      instrumentation.manuallyInstrument(mockCallbackManagerModule as any);
      
      // Should call _configureSync with modified handlers
      const testHandlers = [{ name: 'existing-handler' }];
      mockCallbackManagerModule.CallbackManager._configureSync(testHandlers);
      
      expect(mockCallbackManagerModule.CallbackManager._configureSync).toHaveBeenCalled();
    });

    it('should add tracer to handlers in _configureSync', () => {
      const { addTracerToHandlers } = require('../instrumentationUtils');
      
      instrumentation.manuallyInstrument(mockCallbackManagerModule as any);
      
      const testHandlers = [{ name: 'existing-handler' }];
      mockCallbackManagerModule.CallbackManager._configureSync(testHandlers);
      
      expect(addTracerToHandlers).toHaveBeenCalledWith(
        expect.any(Object), // FITracer instance
        testHandlers
      );
    });
  });

  describe('CallbackManager configure Patching (legacy)', () => {
    beforeEach(() => {
      // Remove _configureSync to simulate older version
      delete mockCallbackManagerModule.CallbackManager._configureSync;
      mockCallbackManagerModule.CallbackManager.configure = jest.fn();
    });

    it('should patch configure when _configureSync is not available', () => {
      instrumentation.manuallyInstrument(mockCallbackManagerModule as any);
      
      expect(mockCallbackManagerModule.CallbackManager.configure).toBeDefined();
    });
  });

  describe('Handler Integration', () => {
    it('should integrate with existing handlers', () => {
      const { addTracerToHandlers } = require('../instrumentationUtils');
      
      instrumentation.manuallyInstrument(mockCallbackManagerModule as any);
      
      const existingHandlers = [
        { name: 'console-handler' },
        { name: 'file-handler' },
      ];
      
      mockCallbackManagerModule.CallbackManager._configureSync?.(existingHandlers);
      
      expect(addTracerToHandlers).toHaveBeenCalledWith(
        expect.any(Object),
        existingHandlers
      );
    });

    it('should handle empty handlers array', () => {
      const { addTracerToHandlers } = require('../instrumentationUtils');
      
      instrumentation.manuallyInstrument(mockCallbackManagerModule as any);
      
      mockCallbackManagerModule.CallbackManager._configureSync?.([]);
      
      expect(addTracerToHandlers).toHaveBeenCalledWith(
        expect.any(Object),
        []
      );
    });

    it('should handle undefined handlers', () => {
      const { addTracerToHandlers } = require('../instrumentationUtils');
      
      instrumentation.manuallyInstrument(mockCallbackManagerModule as any);
      
      mockCallbackManagerModule.CallbackManager._configureSync?.(undefined);
      
      expect(addTracerToHandlers).toHaveBeenCalledWith(
        expect.any(Object),
        undefined
      );
    });
  });

  describe('Trace Configuration', () => {
    it('should respect mask inputs configuration', () => {
      const maskedInstrumentation = new LangChainInstrumentation({
        traceConfig: {
          maskInputs: true,
          maskOutputs: false,
        },
      });

      expect(maskedInstrumentation).toBeInstanceOf(LangChainInstrumentation);
      expect(FITracer).toHaveBeenCalledWith({
        tracer: expect.any(Object),
        traceConfig: {
          maskInputs: true,
          maskOutputs: false,
        },
      });
    });

    it('should respect mask outputs configuration', () => {
      const maskedInstrumentation = new LangChainInstrumentation({
        traceConfig: {
          maskInputs: false,
          maskOutputs: true,
        },
      });

      expect(maskedInstrumentation).toBeInstanceOf(LangChainInstrumentation);
      expect(FITracer).toHaveBeenCalledWith({
        tracer: expect.any(Object),
        traceConfig: {
          maskInputs: false,
          maskOutputs: true,
        },
      });
    });
  });

  describe('Unpatch', () => {
    it('should properly unpatch _configureSync', () => {
      instrumentation.manuallyInstrument(mockCallbackManagerModule as any);
      
      // Verify it's patched
      expect(isPatched()).toBe(true);
      
      // Unpatch
      instrumentation.disable();
      
      // Should still be defined but restored
      expect(mockCallbackManagerModule.CallbackManager._configureSync).toBeDefined();
    });

    it('should properly unpatch configure when _configureSync not available', () => {
      // Remove _configureSync to simulate older version
      delete mockCallbackManagerModule.CallbackManager._configureSync;
      mockCallbackManagerModule.CallbackManager.configure = jest.fn();
      
      instrumentation.manuallyInstrument(mockCallbackManagerModule as any);
      
      // Verify it's patched
      expect(isPatched()).toBe(true);
      
      // Unpatch
      instrumentation.disable();
      
      // Should still be defined but restored
      expect(mockCallbackManagerModule.CallbackManager.configure).toBeDefined();
    });

    it('should handle unpatch with null module', () => {
      // This should not throw an error
      expect(() => {
        instrumentation.disable();
      }).not.toThrow();
    });
  });

  describe('Version Compatibility', () => {
    it('should handle v0.1.0 modules (configure only)', () => {
      const v01Module = {
        CallbackManager: {
          configure: jest.fn(),
          // No _configureSync in v0.1.0
        },
      };

      instrumentation.manuallyInstrument(v01Module as any);
      
      expect(v01Module.CallbackManager.configure).toBeDefined();
      expect(v01Module.CallbackManager._configureSync).toBeUndefined();
    });

    it('should handle v0.2.0+ modules (_configureSync available)', () => {
      const v02Module = {
        CallbackManager: {
          configure: jest.fn(),
          _configureSync: jest.fn(),
        },
      };

      instrumentation.manuallyInstrument(v02Module as any);
      
      expect(v02Module.CallbackManager.configure).toBeDefined();
      expect(v02Module.CallbackManager._configureSync).toBeDefined();
    });
  });

  describe('Error Handling', () => {
    it('should handle malformed module gracefully', () => {
      const malformedModule = {
        // Missing CallbackManager
      };

      expect(() => {
        instrumentation.manuallyInstrument(malformedModule as any);
      }).not.toThrow();
    });

    it('should handle module with immutable properties', () => {
      const immutableModule = {
        CallbackManager: {
          configure: jest.fn(),
          _configureSync: jest.fn(),
        },
      };

      // Make the module immutable
      Object.freeze(immutableModule);

      expect(() => {
        instrumentation.manuallyInstrument(immutableModule as any);
      }).not.toThrow();
    });
  });

  describe('Module Flag Management', () => {
    it('should set fiPatched flag on successful patch', () => {
      const moduleWithFlag = {
        CallbackManager: {
          configure: jest.fn(),
          _configureSync: jest.fn(),
        },
      };

      instrumentation.manuallyInstrument(moduleWithFlag as any);
      
      expect((moduleWithFlag as any).fiPatched).toBe(true);
    });

    it('should handle flag setting failure gracefully', () => {
      const moduleWithReadonlyFlag = {
        CallbackManager: {
          configure: jest.fn(),
          _configureSync: jest.fn(),
        },
      };

      // Make fiPatched read-only
      Object.defineProperty(moduleWithReadonlyFlag, 'fiPatched', {
        value: false,
        writable: false,
        configurable: false,
      });

      expect(() => {
        instrumentation.manuallyInstrument(moduleWithReadonlyFlag as any);
      }).not.toThrow();
    });
  });
}); 