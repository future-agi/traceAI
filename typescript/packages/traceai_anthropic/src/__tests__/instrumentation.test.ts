import { describe, it, expect, jest, beforeEach, afterEach } from '@jest/globals';
import { AnthropicInstrumentation, isPatched } from '../instrumentation';
import { trace, SpanKind, SpanStatusCode } from '@opentelemetry/api';
import { FITracer } from '@traceai/fi-core';

// Mock Anthropic SDK
const mockAnthropic = {
  Messages: {
    prototype: {
      create: jest.fn(),
    },
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
      return (fn as any)(mockSpan);
    }),
  })),
  safelyJSONStringify: jest.fn((obj) => JSON.stringify(obj)),
  TraceConfigOptions: {},
}));

describe('Anthropic Instrumentation', () => {
  let instrumentation: AnthropicInstrumentation;
  let mockTracer: any;

  beforeEach(() => {
    jest.clearAllMocks();
    
    // Create a mock tracer
    mockTracer = {
      startSpan: jest.fn().mockReturnValue((global as any).testUtils.createMockSpan()),
      startActiveSpan: jest.fn((name, fn) => fn((global as any).testUtils.createMockSpan())),
    };

    instrumentation = new AnthropicInstrumentation({
      instrumentationConfig: {
        enabled: true,
      },
      traceConfig: {} as any,
    });

    // Mock the tracer property
    Object.defineProperty(instrumentation, 'tracer', {
      get: () => mockTracer,
      configurable: true,
    });
  });

  describe('Initialization', () => {
    it('should create instrumentation instance', () => {
      expect(instrumentation).toBeInstanceOf(AnthropicInstrumentation);
    });

    it('should initialize with default config', () => {
      const defaultInstrumentation = new AnthropicInstrumentation();
      expect(defaultInstrumentation).toBeInstanceOf(AnthropicInstrumentation);
    });

    it('should initialize with custom config', () => {
      const customInstrumentation = new AnthropicInstrumentation({
        instrumentationConfig: {
          enabled: false,
        },
        traceConfig: {} as any,
      });
      expect(customInstrumentation).toBeInstanceOf(AnthropicInstrumentation);
    });
  });

  describe('Patching', () => {
    it('should report not patched initially', () => {
      expect(isPatched()).toBe(false);
    });

    it('should patch Anthropic module', () => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(mockAnthropic as any);
      
      expect(mockAnthropic.Messages.prototype.create).toBeDefined();
    });

    it('should not double-patch', () => {
      instrumentation.enable();
      
      // First patch
      instrumentation.manuallyInstrument(mockAnthropic as any);
      const originalCreate = mockAnthropic.Messages.prototype.create;
      
      // Second patch attempt
      instrumentation.manuallyInstrument(mockAnthropic as any);
      
      expect(mockAnthropic.Messages.prototype.create).toBe(originalCreate);
    });
  });

  describe('Messages Instrumentation', () => {
    beforeEach(() => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(mockAnthropic as any);
    });

    it('should instrument messages create', async () => {
      const mockResponse = {
        id: 'msg_123',
        type: 'message',
        role: 'assistant',
        content: [
          {
            type: 'text',
            text: 'Hello! How can I help you today?',
          },
        ],
        model: 'claude-3-sonnet-20240229',
        stop_reason: 'end_turn',
        stop_sequence: null,
        usage: {
          input_tokens: 12,
          output_tokens: 10,
        },
      };

      mockAnthropic.Messages.prototype.create.mockResolvedValue(mockResponse);

      const messages = mockAnthropic.Messages.prototype;
      
      const result = await messages.create({
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [
          { role: 'user', content: 'Hello!' },
        ],
      });

      expect(result).toEqual(mockResponse);
      expect(FITracer).toHaveBeenCalled();
    });

    it('should handle streaming messages', async () => {
      const mockStream = {
        tee: jest.fn().mockReturnValue([
          {
            [Symbol.asyncIterator]: async function* () {
              yield {
                type: 'message_start',
                message: {
                  id: 'msg_123',
                  type: 'message',
                  role: 'assistant',
                  content: [],
                  model: 'claude-3-sonnet-20240229',
                  stop_reason: null,
                  stop_sequence: null,
                  usage: { input_tokens: 12, output_tokens: 0 },
                },
              };
              yield {
                type: 'content_block_delta',
                index: 0,
                delta: { type: 'text_delta', text: 'Hello' },
              };
              yield {
                type: 'content_block_delta',
                index: 0,
                delta: { type: 'text_delta', text: '!' },
              };
              yield {
                type: 'message_delta',
                delta: { stop_reason: 'end_turn' },
                usage: { output_tokens: 10 },
              };
            },
          },
          {
            [Symbol.asyncIterator]: async function* () {
              // User stream (empty for simplicity)
            },
          },
        ]),
      };

      (mockAnthropic.Messages.prototype.create as any).mockResolvedValue(mockStream);

      const messages = mockAnthropic.Messages.prototype;
      
      const result = await messages.create({
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [
          { role: 'user', content: 'Hello!' },
        ],
        stream: true,
      });

      expect(result).toEqual(mockStream);
      expect(FITracer).toHaveBeenCalled();
    });

    it('should handle messages with tool calls', async () => {
      const mockResponse = {
        id: 'msg_123',
        type: 'message',
        role: 'assistant',
        content: [
          {
            type: 'tool_use',
            id: 'toolu_123',
            name: 'get_weather',
            input: { location: 'San Francisco' },
          },
        ],
        model: 'claude-3-sonnet-20240229',
        stop_reason: 'tool_use',
        stop_sequence: null,
        usage: {
          input_tokens: 20,
          output_tokens: 15,
        },
      };

      mockAnthropic.Messages.prototype.create.mockResolvedValue(mockResponse);

      const messages = mockAnthropic.Messages.prototype;
      
      const result = await messages.create({
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [
          { role: 'user', content: 'What\'s the weather in San Francisco?' },
        ],
        tools: [
          {
            name: 'get_weather',
            description: 'Get weather information for a location',
            input_schema: {
              type: 'object',
              properties: {
                location: { type: 'string', description: 'The location to get weather for' },
              },
              required: ['location'],
            },
          },
        ],
      });

      expect(result).toEqual(mockResponse);
      expect(FITracer).toHaveBeenCalled();
    });

    it('should handle system messages', async () => {
      const mockResponse = {
        id: 'msg_123',
        type: 'message',
        role: 'assistant',
        content: [
          {
            type: 'text',
            text: 'I understand I should be helpful and harmless.',
          },
        ],
        model: 'claude-3-sonnet-20240229',
        stop_reason: 'end_turn',
        stop_sequence: null,
        usage: {
          input_tokens: 25,
          output_tokens: 12,
        },
      };

      mockAnthropic.Messages.prototype.create.mockResolvedValue(mockResponse);

      const messages = mockAnthropic.Messages.prototype;
      
      const result = await messages.create({
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        system: 'You are a helpful and harmless AI assistant.',
        messages: [
          { role: 'user', content: 'Hello!' },
        ],
      });

      expect(result).toEqual(mockResponse);
      expect(FITracer).toHaveBeenCalled();
    });

    it('should handle messages creation errors', async () => {
      const mockError = new Error('API Error');
      mockAnthropic.Messages.prototype.create.mockRejectedValue(mockError);

      const messages = mockAnthropic.Messages.prototype;
      
      await expect(
        messages.create({
          model: 'claude-3-sonnet-20240229',
          max_tokens: 1000,
          messages: [{ role: 'user', content: 'Hello!' }],
        })
      ).rejects.toThrow('API Error');

      expect(FITracer).toHaveBeenCalled();
    });
  });

  describe('Error Handling', () => {
    beforeEach(() => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(mockAnthropic as any);
    });

    it('should handle API errors gracefully', async () => {
      const apiError = new Error('Invalid API key');
      mockAnthropic.Messages.prototype.create.mockRejectedValue(apiError);

      const messages = mockAnthropic.Messages.prototype;
      
      await expect(
        messages.create({
          model: 'claude-3-sonnet-20240229',
          max_tokens: 1000,
          messages: [{ role: 'user', content: 'Hello!' }],
        })
      ).rejects.toThrow('Invalid API key');

      expect(FITracer).toHaveBeenCalled();
    });

    it('should handle rate limit errors', async () => {
      const rateLimitError = new Error('Rate limit exceeded');
      mockAnthropic.Messages.prototype.create.mockRejectedValue(rateLimitError);

      const messages = mockAnthropic.Messages.prototype;
      
      await expect(
        messages.create({
          model: 'claude-3-sonnet-20240229',
          max_tokens: 1000,
          messages: [{ role: 'user', content: 'Hello!' }],
        })
      ).rejects.toThrow('Rate limit exceeded');

      expect(FITracer).toHaveBeenCalled();
    });

    it('should handle network errors', async () => {
      const networkError = new Error('Network timeout');
      mockAnthropic.Messages.prototype.create.mockRejectedValue(networkError);

      const messages = mockAnthropic.Messages.prototype;
      
      await expect(
        messages.create({
          model: 'claude-3-sonnet-20240229',
          max_tokens: 1000,
          messages: [{ role: 'user', content: 'Hello!' }],
        })
      ).rejects.toThrow('Network timeout');

      expect(FITracer).toHaveBeenCalled();
    });
  });

  describe('Trace Configuration', () => {
    it('should respect mask inputs configuration', () => {
      const maskedInstrumentation = new AnthropicInstrumentation({
        traceConfig: {
          maskInputs: true,
          maskOutputs: false,
        },
      });

      expect(maskedInstrumentation).toBeInstanceOf(AnthropicInstrumentation);
      expect((maskedInstrumentation as any)._traceConfig.maskInputs).toBe(true);
    });

    it('should respect mask outputs configuration', () => {
      const maskedInstrumentation = new AnthropicInstrumentation({
        traceConfig: {
          maskInputs: false,
          maskOutputs: true,
        },
      });

      expect(maskedInstrumentation).toBeInstanceOf(AnthropicInstrumentation);
      expect((maskedInstrumentation as any)._traceConfig.maskOutputs).toBe(true);
    });
  });

  describe('Multiple Content Types', () => {
    beforeEach(() => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(mockAnthropic as any);
    });

    it('should handle mixed content types', async () => {
      const mockResponse = {
        id: 'msg_123',
        type: 'message',
        role: 'assistant',
        content: [
          {
            type: 'text',
            text: 'Here is the weather information:',
          },
          {
            type: 'tool_use',
            id: 'toolu_123',
            name: 'get_weather',
            input: { location: 'San Francisco' },
          },
        ],
        model: 'claude-3-sonnet-20240229',
        stop_reason: 'tool_use',
        stop_sequence: null,
        usage: {
          input_tokens: 30,
          output_tokens: 20,
        },
      };

      mockAnthropic.Messages.prototype.create.mockResolvedValue(mockResponse);

      const messages = mockAnthropic.Messages.prototype;
      
      const result = await messages.create({
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [
          { role: 'user', content: 'What\'s the weather in San Francisco?' },
        ],
        tools: [
          {
            name: 'get_weather',
            description: 'Get weather information for a location',
            input_schema: {
              type: 'object',
              properties: {
                location: { type: 'string' },
              },
            },
          },
        ],
      });

      expect(result).toEqual(mockResponse);
      expect(FITracer).toHaveBeenCalled();
    });
  });

  describe('Unpatch', () => {
    beforeEach(() => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(mockAnthropic as any);
    });

    it('should disable instrumentation', () => {
      instrumentation.disable();
      
      // After disabling, the original methods should be restored
      expect(mockAnthropic.Messages.prototype.create).toBeDefined();
    });
  });

  describe('Module Detection', () => {
    it('should handle missing Messages prototype', () => {
      const incompleteModule = {};
      
      // This should not throw an error
      instrumentation.enable();
      const result = instrumentation.manuallyInstrument(incompleteModule as any);
      
      expect(result).toBe(incompleteModule);
    });
  });
}); 