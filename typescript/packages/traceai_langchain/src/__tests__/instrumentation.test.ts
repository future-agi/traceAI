import { describe, it, expect, jest, beforeEach, afterEach } from '@jest/globals';
let LangChainInstrumentation: any;
let isPatched: any;
let _resetPatchedStateForTesting: any;
let FITracer: any;

// Mock LangChain CallbackManager module — fresh mocks are created in beforeEach
let mockCallbackManagerModule: any;

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
    startActiveSpan: jest.fn((name: any, fn: any) => {
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
      return (fn as any)(mockSpan);
    }),
  })),
  TraceConfigOptions: {},
}));

// Mock addTracerToHandlers — use the path as it appears in the source file's import
jest.mock('../instrumentationUtils', () => ({
  addTracerToHandlers: jest.fn((tracer: any, handlers: any) => {
    const list = Array.isArray(handlers) ? handlers : [];
    return [...list, { tracer }];
  }),
}));

// Now import after mocks so the implementation uses mocked dependencies
({ LangChainInstrumentation, isPatched, _resetPatchedStateForTesting } = require('../instrumentation'));
({ FITracer } = require('@traceai/fi-core'));

describe('LangChain Instrumentation', () => {
  let instrumentation: any;
  let mockTracer: any;

  beforeEach(() => {
    jest.clearAllMocks();
    _resetPatchedStateForTesting();

    // Create a fresh mock module for each test to avoid cross-test contamination
    mockCallbackManagerModule = {
      CallbackManager: {
        configure: jest.fn(),
        _configureSync: jest.fn(),
      },
    };

    // Create a mock tracer
    mockTracer = {
      startSpan: jest.fn().mockReturnValue((global as any).testUtils.createMockSpan()),
      startActiveSpan: jest.fn((name: any, fn: any) => (fn as any)((global as any).testUtils.createMockSpan())),
    };

    instrumentation = new LangChainInstrumentation({
      instrumentationConfig: {
        enabled: true,
      },
      traceConfig: {
        maskInputs: false,
        maskOutputs: false,
      } as any,
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
        } as any,
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
    let originalConfigureSync: jest.Mock;

    beforeEach(() => {
      // Keep a reference to the original mock before patching replaces it
      originalConfigureSync = mockCallbackManagerModule.CallbackManager._configureSync;
    });

    it('should patch _configureSync when available', () => {
      instrumentation.manuallyInstrument(mockCallbackManagerModule as any);

      // Should call _configureSync with modified handlers
      const testHandlers = [{ name: 'existing-handler' }];
      mockCallbackManagerModule.CallbackManager._configureSync(testHandlers);

      // The original should have been called (via the wrapper)
      expect(originalConfigureSync).toHaveBeenCalled();
    });

    it('should add tracer to handlers in _configureSync', () => {
      instrumentation.manuallyInstrument(mockCallbackManagerModule as any);
      const testHandlers = [{ name: 'existing-handler' }];
      mockCallbackManagerModule.CallbackManager._configureSync(testHandlers);
      // The original jest.fn is called with the modified handlers (tracer appended by the mock)
      const calledWith = originalConfigureSync.mock.calls[0][0] as any[];
      expect(Array.isArray(calledWith)).toBe(true);
      expect(calledWith.length).toBe(testHandlers.length + 1);
    });
  });

  describe('CallbackManager configure Patching (legacy)', () => {
    beforeEach(() => {
      // Remove _configureSync to simulate older version
      delete (mockCallbackManagerModule.CallbackManager as any)._configureSync;
      mockCallbackManagerModule.CallbackManager.configure = jest.fn();
    });

    it('should patch configure when _configureSync is not available', () => {
      instrumentation.manuallyInstrument(mockCallbackManagerModule as any);

      expect(mockCallbackManagerModule.CallbackManager.configure).toBeDefined();
    });
  });

  describe('Handler Integration', () => {
    let originalConfigureSync: jest.Mock;

    beforeEach(() => {
      // Keep a reference to the original mock before patching replaces it
      originalConfigureSync = mockCallbackManagerModule.CallbackManager._configureSync;
    });

    it('should integrate with existing handlers', () => {
      instrumentation.manuallyInstrument(mockCallbackManagerModule as any);
      const existingHandlers = [
        { name: 'console-handler' },
        { name: 'file-handler' },
      ];
      mockCallbackManagerModule.CallbackManager._configureSync(existingHandlers);
      // Check the original mock was called with the modified handlers
      const calledWith = originalConfigureSync.mock.calls[0][0] as any[];
      expect(Array.isArray(calledWith)).toBe(true);
      expect(calledWith.length).toBe(existingHandlers.length + 1);
    });

    it('should handle empty handlers array', () => {
      instrumentation.manuallyInstrument(mockCallbackManagerModule as any);
      mockCallbackManagerModule.CallbackManager._configureSync([]);
      const calledWith = originalConfigureSync.mock.calls[0][0] as any[];
      expect(Array.isArray(calledWith)).toBe(true);
      expect(calledWith.length).toBe(1);
    });

    it('should handle undefined handlers', () => {
      instrumentation.manuallyInstrument(mockCallbackManagerModule as any);
      mockCallbackManagerModule.CallbackManager._configureSync(undefined);
      const calledWith = originalConfigureSync.mock.calls[0][0] as any[];
      expect(Array.isArray(calledWith)).toBe(true);
      expect(calledWith.length).toBe(1);
    });
  });

  describe('Trace Configuration', () => {
    it('should respect mask inputs configuration', () => {
      const maskedInstrumentation = new LangChainInstrumentation({
        traceConfig: {
          maskInputs: true,
          maskOutputs: false,
        } as any,
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
        } as any,
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
      expect(isPatched()).toBe(true);
      instrumentation.disable();
      expect(mockCallbackManagerModule.CallbackManager._configureSync).toBeDefined();
    });

    it('should properly unpatch configure when _configureSync not available', () => {
      // Remove _configureSync to simulate older version
      delete (mockCallbackManagerModule.CallbackManager as any)._configureSync;
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
      expect((v01Module as any).CallbackManager._configureSync).toBeUndefined();
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
      const moduleWithFlag: any = {
        CallbackManager: {
          configure: jest.fn(),
          _configureSync: jest.fn(),
        },
      };
      // Ensure property is writable
      Object.defineProperty(moduleWithFlag, 'fiPatched', { value: undefined, writable: true, configurable: true });
      instrumentation.manuallyInstrument(moduleWithFlag);
      expect(moduleWithFlag.fiPatched).toBe(true);
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