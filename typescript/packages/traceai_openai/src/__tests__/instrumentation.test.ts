import { describe, it, expect, jest, beforeEach, afterEach } from '@jest/globals';
import { OpenAIInstrumentation, isPatched } from '../instrumentation';
import { trace, SpanKind, SpanStatusCode } from '@opentelemetry/api';
import { NodeSDK } from '@opentelemetry/sdk-node';
import { FITracer } from '@traceai/fi-core';

// Mock OpenAI
const mockOpenAI = {
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
  safelyJSONStringify: jest.fn((obj) => JSON.stringify(obj)),
  TraceConfigOptions: {},
}));

describe('OpenAI Instrumentation', () => {
  let instrumentation: OpenAIInstrumentation;
  let mockTracer: any;

  beforeEach(() => {
    jest.clearAllMocks();
    
    // Create a mock tracer
    mockTracer = {
      startSpan: jest.fn().mockReturnValue(global.testUtils.createMockSpan()),
      startActiveSpan: jest.fn((name, fn) => fn(global.testUtils.createMockSpan())),
    };

    instrumentation = new OpenAIInstrumentation({
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
      expect(instrumentation).toBeInstanceOf(OpenAIInstrumentation);
    });

    it('should initialize with default config', () => {
      const defaultInstrumentation = new OpenAIInstrumentation();
      expect(defaultInstrumentation).toBeInstanceOf(OpenAIInstrumentation);
    });

    it('should initialize with custom config', () => {
      const customInstrumentation = new OpenAIInstrumentation({
        instrumentationConfig: {
          enabled: false,
        },
        traceConfig: {
          maskInputs: true,
          maskOutputs: true,
        },
      });
      expect(customInstrumentation).toBeInstanceOf(OpenAIInstrumentation);
    });
  });

  describe('Patching', () => {
    it('should report not patched initially', () => {
      expect(isPatched()).toBe(false);
    });

    it('should patch OpenAI module', () => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(mockOpenAI as any);
      
      expect(mockOpenAI.OpenAI.Chat.Completions.prototype.create).toBeDefined();
    });

    it('should not double-patch', () => {
      instrumentation.enable();
      
      // First patch
      const patchedModule = instrumentation.manuallyInstrument(mockOpenAI as any);
      const originalCreate = mockOpenAI.OpenAI.Chat.Completions.prototype.create;
      
      // Second patch attempt
      const secondPatchedModule = instrumentation.manuallyInstrument(mockOpenAI as any);
      
      expect(mockOpenAI.OpenAI.Chat.Completions.prototype.create).toBe(originalCreate);
    });
  });

  describe('Chat Completions Instrumentation', () => {
    beforeEach(() => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(mockOpenAI as any);
    });

    it('should instrument chat completions create', async () => {
      const mockResponse = {
        id: 'chatcmpl-123',
        object: 'chat.completion',
        created: 1677652288,
        model: 'gpt-3.5-turbo',
        choices: [
          {
            index: 0,
            message: {
              role: 'assistant',
              content: 'Hello! How can I help you today?',
            },
            finish_reason: 'stop',
          },
        ],
        usage: {
          prompt_tokens: 12,
          completion_tokens: 10,
          total_tokens: 22,
        },
      };

      mockOpenAI.OpenAI.Chat.Completions.prototype.create.mockResolvedValue(mockResponse);

      const chatCompletion = mockOpenAI.OpenAI.Chat.Completions.prototype;
      
      const result = await chatCompletion.create({
        model: 'gpt-3.5-turbo',
        messages: [
          { role: 'user', content: 'Hello!' },
        ],
      });

      expect(result).toEqual(mockResponse);
      expect(FITracer).toHaveBeenCalled();
    });

    it('should handle streaming chat completions', async () => {
      const mockStream = {
        [Symbol.asyncIterator]: async function* () {
          yield {
            id: 'chatcmpl-123',
            object: 'chat.completion.chunk',
            created: 1677652288,
            model: 'gpt-3.5-turbo',
            choices: [
              {
                index: 0,
                delta: { content: 'Hello' },
                finish_reason: null,
              },
            ],
          };
          yield {
            id: 'chatcmpl-123',
            object: 'chat.completion.chunk',
            created: 1677652288,
            model: 'gpt-3.5-turbo',
            choices: [
              {
                index: 0,
                delta: { content: '!' },
                finish_reason: 'stop',
              },
            ],
          };
        },
      };

      mockOpenAI.OpenAI.Chat.Completions.prototype.create.mockResolvedValue(mockStream);

      const chatCompletion = mockOpenAI.OpenAI.Chat.Completions.prototype;
      
      const result = await chatCompletion.create({
        model: 'gpt-3.5-turbo',
        messages: [
          { role: 'user', content: 'Hello!' },
        ],
        stream: true,
      });

      expect(result).toEqual(mockStream);
      expect(FITracer).toHaveBeenCalled();
    });

    it('should handle chat completion errors', async () => {
      const mockError = new Error('API Error');
      mockOpenAI.OpenAI.Chat.Completions.prototype.create.mockRejectedValue(mockError);

      const chatCompletion = mockOpenAI.OpenAI.Chat.Completions.prototype;
      
      await expect(
        chatCompletion.create({
          model: 'gpt-3.5-turbo',
          messages: [{ role: 'user', content: 'Hello!' }],
        })
      ).rejects.toThrow('API Error');

      expect(FITracer).toHaveBeenCalled();
    });
  });

  describe('Completions Instrumentation', () => {
    beforeEach(() => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(mockOpenAI as any);
    });

    it('should instrument completions create', async () => {
      const mockResponse = {
        id: 'cmpl-123',
        object: 'text_completion',
        created: 1677652288,
        model: 'gpt-3.5-turbo-instruct',
        choices: [
          {
            text: 'Hello! How can I help you today?',
            index: 0,
            finish_reason: 'stop',
          },
        ],
        usage: {
          prompt_tokens: 5,
          completion_tokens: 10,
          total_tokens: 15,
        },
      };

      mockOpenAI.OpenAI.Completions.prototype.create.mockResolvedValue(mockResponse);

      const completion = mockOpenAI.OpenAI.Completions.prototype;
      
      const result = await completion.create({
        model: 'gpt-3.5-turbo-instruct',
        prompt: 'Hello!',
        max_tokens: 10,
      });

      expect(result).toEqual(mockResponse);
      expect(FITracer).toHaveBeenCalled();
    });
  });

  describe('Embeddings Instrumentation', () => {
    beforeEach(() => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(mockOpenAI as any);
    });

    it('should instrument embeddings create', async () => {
      const mockResponse = {
        object: 'list',
        data: [
          {
            object: 'embedding',
            embedding: [0.1, 0.2, 0.3],
            index: 0,
          },
        ],
        model: 'text-embedding-ada-002',
        usage: {
          prompt_tokens: 8,
          total_tokens: 8,
        },
      };

      mockOpenAI.OpenAI.Embeddings.prototype.create.mockResolvedValue(mockResponse);

      const embeddings = mockOpenAI.OpenAI.Embeddings.prototype;
      
      const result = await embeddings.create({
        model: 'text-embedding-ada-002',
        input: 'Hello world!',
      });

      expect(result).toEqual(mockResponse);
      expect(FITracer).toHaveBeenCalled();
    });

    it('should handle array input for embeddings', async () => {
      const mockResponse = {
        object: 'list',
        data: [
          {
            object: 'embedding',
            embedding: [0.1, 0.2, 0.3],
            index: 0,
          },
          {
            object: 'embedding',
            embedding: [0.4, 0.5, 0.6],
            index: 1,
          },
        ],
        model: 'text-embedding-ada-002',
        usage: {
          prompt_tokens: 16,
          total_tokens: 16,
        },
      };

      mockOpenAI.OpenAI.Embeddings.prototype.create.mockResolvedValue(mockResponse);

      const embeddings = mockOpenAI.OpenAI.Embeddings.prototype;
      
      const result = await embeddings.create({
        model: 'text-embedding-ada-002',
        input: ['Hello world!', 'Goodbye world!'],
      });

      expect(result).toEqual(mockResponse);
      expect(FITracer).toHaveBeenCalled();
    });
  });

  describe('Error Handling', () => {
    beforeEach(() => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(mockOpenAI as any);
    });

    it('should handle API errors gracefully', async () => {
      const apiError = new Error('Invalid API key');
      mockOpenAI.OpenAI.Chat.Completions.prototype.create.mockRejectedValue(apiError);

      const chatCompletion = mockOpenAI.OpenAI.Chat.Completions.prototype;
      
      await expect(
        chatCompletion.create({
          model: 'gpt-3.5-turbo',
          messages: [{ role: 'user', content: 'Hello!' }],
        })
      ).rejects.toThrow('Invalid API key');

      expect(FITracer).toHaveBeenCalled();
    });

    it('should handle network errors', async () => {
      const networkError = new Error('Network timeout');
      mockOpenAI.OpenAI.Chat.Completions.prototype.create.mockRejectedValue(networkError);

      const chatCompletion = mockOpenAI.OpenAI.Chat.Completions.prototype;
      
      await expect(
        chatCompletion.create({
          model: 'gpt-3.5-turbo',
          messages: [{ role: 'user', content: 'Hello!' }],
        })
      ).rejects.toThrow('Network timeout');

      expect(FITracer).toHaveBeenCalled();
    });
  });

  describe('Trace Configuration', () => {
    it('should respect mask inputs configuration', () => {
      const maskedInstrumentation = new OpenAIInstrumentation({
        traceConfig: {
          maskInputs: true,
          maskOutputs: false,
        },
      });

      expect(maskedInstrumentation).toBeInstanceOf(OpenAIInstrumentation);
      expect((maskedInstrumentation as any)._traceConfig.maskInputs).toBe(true);
    });

    it('should respect mask outputs configuration', () => {
      const maskedInstrumentation = new OpenAIInstrumentation({
        traceConfig: {
          maskInputs: false,
          maskOutputs: true,
        },
      });

      expect(maskedInstrumentation).toBeInstanceOf(OpenAIInstrumentation);
      expect((maskedInstrumentation as any)._traceConfig.maskOutputs).toBe(true);
    });
  });

  describe('Unpatch', () => {
    beforeEach(() => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(mockOpenAI as any);
    });

    it('should disable instrumentation', () => {
      instrumentation.disable();
      
      // After disabling, the original methods should be restored
      expect(mockOpenAI.OpenAI.Chat.Completions.prototype.create).toBeDefined();
    });
  });
}); 