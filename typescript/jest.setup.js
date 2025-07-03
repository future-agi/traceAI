// Jest setup file for common test utilities and configuration

// Mock console methods to reduce noise in tests
const originalConsole = global.console;

beforeEach(() => {
  global.console = {
    ...originalConsole,
    // Suppress console.log, console.warn, etc. during tests
    log: jest.fn(),
    warn: jest.fn(),
    info: jest.fn(),
    // Keep error and debug for important information
    error: originalConsole.error,
    debug: originalConsole.debug,
  };
});

afterEach(() => {
  global.console = originalConsole;
});

// Increase timeout for integration tests
jest.setTimeout(30000);

// Common test utilities
global.testUtils = {
  // Helper to create mock spans
  createMockSpan: () => ({
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
  
  // Helper to create mock tracer
  createMockTracer: () => ({
    startSpan: jest.fn().mockReturnValue(global.testUtils.createMockSpan()),
    startActiveSpan: jest.fn((name, fn) => fn(global.testUtils.createMockSpan())),
  }),
  
  // Common test data
  mockApiKey: 'test-api-key-123',
  mockModel: 'gpt-3.5-turbo',
  mockPrompt: 'Test prompt for LLM',
  mockResponse: 'Test response from LLM',
}; 