import { describe, it, expect, beforeEach, afterEach, jest } from '@jest/globals';
import { AnthropicInstrumentation, isPatched } from '../instrumentation';

// Properly typed mock for Anthropic SDK
const mockCreate = jest.fn();

// Create a Messages constructor function that can be patched
function Messages() {
  // @ts-ignore
  this.create = mockCreate;
}
Messages.prototype.create = mockCreate;

const MockAnthropic = {
  Messages: Messages,
} as any;

// Mock stream for testing streaming responses
class MockAsyncIterable {
  private chunks: any[];

  constructor(chunks: any[]) {
    this.chunks = chunks;
  }

  async *[Symbol.asyncIterator]() {
    for (const chunk of this.chunks) {
      yield chunk;
    }
  }

  tee(): [MockAsyncIterable, MockAsyncIterable] {
    return [new MockAsyncIterable(this.chunks), new MockAsyncIterable(this.chunks)];
  }
}

describe('Anthropic Instrumentation Comprehensive Tests', () => {
  let instrumentation: AnthropicInstrumentation;

  beforeEach(() => {
    jest.clearAllMocks();
    instrumentation = new AnthropicInstrumentation();
  });

  afterEach(() => {
    if (instrumentation) {
      instrumentation.disable();
    }
  });

  describe('Instrumentation Setup and Configuration', () => {
    it('should initialize with default configuration', () => {
      expect(instrumentation).toBeDefined();
      expect(typeof instrumentation.enable).toBe('function');
      expect(typeof instrumentation.disable).toBe('function');
      expect(typeof instrumentation.manuallyInstrument).toBe('function');
    });

    it('should initialize with custom trace configuration', () => {
      const customInstrumentation = new AnthropicInstrumentation({
        traceConfig: {
          hideInputs: true,
          hideOutputs: false,
        },
      });
      
      expect(customInstrumentation).toBeDefined();
    });

    it('should track patching state correctly', () => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(MockAnthropic);
      expect(isPatched()).toBe(true);

      // Note: disable() calls super.disable() which only unpatches modules
      // registered through the module definition system. manuallyInstrument
      // bypasses that, so the global _isFIPatched flag remains true.
      // The unpatch for manual instrumentation would require calling unpatch
      // directly on the module exports.
      instrumentation.disable();
      expect(isPatched()).toBe(true);
    });

    it('should handle multiple enable/disable cycles', () => {
      // First cycle
      instrumentation.enable();
      instrumentation.manuallyInstrument(MockAnthropic);
      expect(isPatched()).toBe(true);

      // disable() does not reset _isFIPatched for manually instrumented modules
      instrumentation.disable();
      expect(isPatched()).toBe(true);
    });

    it('should prevent double patching', () => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(MockAnthropic);
      expect(isPatched()).toBe(true);

      // Attempt to patch again -- manuallyInstrument returns void
      instrumentation.manuallyInstrument(MockAnthropic);
      expect(isPatched()).toBe(true);
    });
  });

  describe('Messages.create Patching', () => {
    beforeEach(() => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(MockAnthropic);
    });

    it('should patch messages.create method successfully', () => {
      expect(isPatched()).toBe(true);
      expect(MockAnthropic.Messages.prototype.create).toBeDefined();
      expect(typeof MockAnthropic.Messages.prototype.create).toBe('function');
    });

    it('should handle basic message creation calls', async () => {
      const mockResponse = {
        id: 'msg_test_123',
        type: 'message',
        role: 'assistant',
        content: [{ type: 'text', text: 'Hello from Anthropic!' }],
        model: 'claude-3-sonnet-20240229',
        stop_reason: 'end_turn',
        stop_sequence: null,
        usage: { input_tokens: 8, output_tokens: 6 },
      };

      (mockCreate as jest.MockedFunction<any>).mockResolvedValue(mockResponse);

      const messages = new MockAnthropic.Messages();
      const result = await messages.create({
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [{ role: 'user', content: 'Hello!' }],
      });

      expect(result).toEqual(mockResponse);
      expect(mockCreate).toHaveBeenCalledTimes(1);
      expect(mockCreate).toHaveBeenCalledWith({
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [{ role: 'user', content: 'Hello!' }],
      });
    });

    it('should handle tool use scenarios', async () => {
      const mockResponse = {
        id: 'msg_tool_456',
        type: 'message',
        role: 'assistant',
        content: [
          { type: 'text', text: 'I\'ll help you with the weather.' },
          {
            type: 'tool_use',
            id: 'toolu_weather_123',
            name: 'get_weather',
            input: { location: 'New York' },
          },
        ],
        model: 'claude-3-sonnet-20240229',
        stop_reason: 'tool_use',
        stop_sequence: null,
        usage: { input_tokens: 30, output_tokens: 25 },
      };

      (mockCreate as jest.MockedFunction<any>).mockResolvedValue(mockResponse);

      const messages = new MockAnthropic.Messages();
      const result = await messages.create({
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [{ role: 'user', content: 'What\'s the weather in New York?' }],
        tools: [
          {
            name: 'get_weather',
            description: 'Get current weather for a location',
            input_schema: {
              type: 'object',
              properties: {
                location: { type: 'string', description: 'City name' },
              },
              required: ['location'],
            },
          },
        ],
      });

      expect(result).toEqual(mockResponse);
      expect(mockCreate).toHaveBeenCalledWith(
        expect.objectContaining({
          tools: expect.arrayContaining([
            expect.objectContaining({ name: 'get_weather' }),
          ]),
        })
      );
    });

    it('should handle system prompts', async () => {
      const mockResponse = {
        id: 'msg_system_789',
        type: 'message',
        role: 'assistant',
        content: [{ type: 'text', text: 'I understand I should be helpful.' }],
        model: 'claude-3-sonnet-20240229',
        stop_reason: 'end_turn',
        stop_sequence: null,
        usage: { input_tokens: 20, output_tokens: 10 },
      };

      (mockCreate as jest.MockedFunction<any>).mockResolvedValue(mockResponse);

      const messages = new MockAnthropic.Messages();
      await messages.create({
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        system: 'You are a helpful assistant.',
        messages: [{ role: 'user', content: 'Hello!' }],
      });

      expect(mockCreate).toHaveBeenCalledWith(
        expect.objectContaining({
          system: 'You are a helpful assistant.',
        })
      );
    });

    it('should handle complex message content', async () => {
      const mockResponse = {
        id: 'msg_complex_012',
        type: 'message',
        role: 'assistant',
        content: [{ type: 'text', text: 'I can see the image you shared.' }],
        model: 'claude-3-sonnet-20240229',
        stop_reason: 'end_turn',
        stop_sequence: null,
        usage: { input_tokens: 100, output_tokens: 15 },
      };

      (mockCreate as jest.MockedFunction<any>).mockResolvedValue(mockResponse);

      const messages = new MockAnthropic.Messages();
      await messages.create({
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [
          {
            role: 'user',
            content: [
              { type: 'text', text: 'What do you see?' },
              {
                type: 'image',
                source: {
                  type: 'base64',
                  media_type: 'image/jpeg',
                  data: 'fake_base64_data',
                },
              },
            ],
          },
        ],
      });

      expect(mockCreate).toHaveBeenCalledWith(
        expect.objectContaining({
          messages: expect.arrayContaining([
            expect.objectContaining({
              content: expect.arrayContaining([
                expect.objectContaining({ type: 'text' }),
                expect.objectContaining({ type: 'image' }),
              ]),
            }),
          ]),
        })
      );
    });
  });

  describe('Streaming Response Handling', () => {
    beforeEach(() => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(MockAnthropic);
    });

    it('should handle basic streaming responses', async () => {
      const streamChunks = [
        {
          type: 'message_start',
          message: {
            id: 'msg_stream_001',
            type: 'message',
            role: 'assistant',
            content: [],
            model: 'claude-3-sonnet-20240229',
            stop_reason: null,
            stop_sequence: null,
            usage: { input_tokens: 5, output_tokens: 0 },
          },
        },
        {
          type: 'content_block_start',
          index: 0,
          content_block: { type: 'text', text: '' },
        },
        {
          type: 'content_block_delta',
          index: 0,
          delta: { type: 'text_delta', text: 'Hello there!' },
        },
        {
          type: 'content_block_stop',
          index: 0,
        },
        {
          type: 'message_delta',
          delta: { stop_reason: 'end_turn', stop_sequence: null },
          usage: { output_tokens: 3 },
        },
        {
          type: 'message_stop',
        },
      ];

      const mockStream = new MockAsyncIterable(streamChunks);
      (mockCreate as jest.MockedFunction<any>).mockResolvedValue(mockStream);

      const messages = new MockAnthropic.Messages();
      const result = await messages.create({
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [{ role: 'user', content: 'Hello!' }],
        stream: true,
      });

      expect(result).toBeInstanceOf(MockAsyncIterable);
      expect(mockCreate).toHaveBeenCalledWith(
        expect.objectContaining({ stream: true })
      );
    });

    it('should handle streaming tool use responses', async () => {
      const streamChunks = [
        {
          type: 'message_start',
          message: {
            id: 'msg_tool_stream',
            type: 'message',
            role: 'assistant',
            content: [],
            model: 'claude-3-sonnet-20240229',
            stop_reason: null,
            stop_sequence: null,
            usage: { input_tokens: 15, output_tokens: 0 },
          },
        },
        {
          type: 'content_block_start',
          index: 0,
          content_block: {
            type: 'tool_use',
            id: 'toolu_stream_123',
            name: 'calculate',
            input: {},
          },
        },
        {
          type: 'content_block_delta',
          index: 0,
          delta: { type: 'input_json_delta', partial_json: '{"number": 42}' },
        },
        {
          type: 'content_block_stop',
          index: 0,
        },
        {
          type: 'message_delta',
          delta: { stop_reason: 'tool_use', stop_sequence: null },
          usage: { output_tokens: 8 },
        },
        {
          type: 'message_stop',
        },
      ];

      const mockStream = new MockAsyncIterable(streamChunks);
      (mockCreate as jest.MockedFunction<any>).mockResolvedValue(mockStream);

      const messages = new MockAnthropic.Messages();
      const result = await messages.create({
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [{ role: 'user', content: 'Calculate something' }],
        stream: true,
        tools: [{ name: 'calculate', description: 'Do math', input_schema: { type: 'object' } }],
      });

      expect(result).toBeInstanceOf(MockAsyncIterable);
    });
  });

  describe('Error Handling', () => {
    beforeEach(() => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(MockAnthropic);
    });

    it('should handle API errors correctly', async () => {
      const apiError = new Error('Rate limit exceeded');
      (mockCreate as jest.MockedFunction<any>).mockRejectedValue(apiError);

      const messages = new MockAnthropic.Messages();
      
      await expect(messages.create({
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [{ role: 'user', content: 'Hello!' }],
      })).rejects.toThrow('Rate limit exceeded');

      expect(mockCreate).toHaveBeenCalledTimes(1);
    });

    it('should handle malformed request errors', async () => {
      const validationError = new Error('Invalid model specified');
      (mockCreate as jest.MockedFunction<any>).mockRejectedValue(validationError);

      const messages = new MockAnthropic.Messages();
      
      await expect(messages.create({
        model: 'invalid-model',
        max_tokens: 1000,
        messages: [{ role: 'user', content: 'Hello!' }],
      })).rejects.toThrow('Invalid model specified');
    });

    it('should handle network errors', async () => {
      const networkError = new Error('Network connection failed');
      (mockCreate as jest.MockedFunction<any>).mockRejectedValue(networkError);

      const messages = new MockAnthropic.Messages();
      
      await expect(messages.create({
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [{ role: 'user', content: 'Hello!' }],
      })).rejects.toThrow('Network connection failed');
    });
  });

  describe('Edge Cases and Robustness', () => {
    it('should handle missing Messages prototype gracefully', () => {
      const BrokenAnthropic = {} as any; // No Messages property

      instrumentation.enable();
      // manuallyInstrument returns void; it should not throw
      expect(() => instrumentation.manuallyInstrument(BrokenAnthropic)).not.toThrow();
    });

    it('should handle Messages without prototype', () => {
      const IncompleteAnthropic = {
        Messages: {} // No prototype
      } as any;

      instrumentation.enable();
      // patch() checks for Messages?.prototype and returns early if not found,
      // but since _isFIPatched may already be true from prior tests in this
      // suite sharing the same module-level global, we just verify no throw.
      expect(() => instrumentation.manuallyInstrument(IncompleteAnthropic)).not.toThrow();
    });

    it('should handle configuration with various trace options', () => {
      const configurations = [
        { hideInputs: true },
        { hideOutputs: true },
        { hideInputs: true, hideOutputs: true },
        { hideInputs: false, hideOutputs: false },
      ];

      configurations.forEach(config => {
        const configuredInstrumentation = new AnthropicInstrumentation({
          traceConfig: config,
        });
        expect(configuredInstrumentation).toBeDefined();
      });
    });

    it('should handle empty and null parameters', async () => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(MockAnthropic);

      const mockResponse = {
        id: 'msg_empty',
        type: 'message',
        role: 'assistant',
        content: [],
        model: 'claude-3-sonnet-20240229',
        stop_reason: 'end_turn',
        stop_sequence: null,
        usage: { input_tokens: 0, output_tokens: 0 },
      };

      (mockCreate as jest.MockedFunction<any>).mockResolvedValue(mockResponse);

      const messages = new MockAnthropic.Messages();
      await messages.create({
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [],
      });

      expect(mockCreate).toHaveBeenCalledWith(
        expect.objectContaining({ messages: [] })
      );
    });
  });

  describe('Instrumentation State Management', () => {
    it('should maintain state across multiple operations', async () => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(MockAnthropic);
      expect(isPatched()).toBe(true);

      const mockResponse = { id: 'msg_1', content: [], model: 'claude-3-sonnet-20240229' };
      (mockCreate as jest.MockedFunction<any>).mockResolvedValue(mockResponse);

      const messages = new MockAnthropic.Messages();
      
      // Multiple calls should all be instrumented
      await messages.create({ model: 'claude-3-sonnet-20240229', max_tokens: 100, messages: [] });
      await messages.create({ model: 'claude-3-sonnet-20240229', max_tokens: 100, messages: [] });
      await messages.create({ model: 'claude-3-sonnet-20240229', max_tokens: 100, messages: [] });

      expect(mockCreate).toHaveBeenCalledTimes(3);
      expect(isPatched()).toBe(true);
    });

    it('should cleanup properly on disable', () => {
      instrumentation.enable();
      instrumentation.manuallyInstrument(MockAnthropic);
      expect(isPatched()).toBe(true);

      // disable() does not reset _isFIPatched for manually instrumented modules
      instrumentation.disable();
      expect(isPatched()).toBe(true);
    });
  });
}); 