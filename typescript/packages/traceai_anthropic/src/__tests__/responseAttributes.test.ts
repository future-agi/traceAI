import { describe, it, expect } from '@jest/globals';
import {
  getAnthropicInputMessagesAttributes,
  getAnthropicOutputMessagesAttributes,
  getAnthropicUsageAttributes,
  getAnthropicToolsAttributes,
  aggregateAnthropicStreamEvents,
} from '../responseAttributes';
import { SemanticConventions } from '@traceai/fi-semantic-conventions';

// Mock the safelyJSONStringify function
jest.mock('@traceai/fi-core', () => ({
  safelyJSONStringify: jest.fn((obj) => JSON.stringify(obj)),
}));

describe('Anthropic Response Attributes', () => {
  describe('getAnthropicInputMessagesAttributes', () => {
    it('should extract attributes from simple text messages', () => {
      const params = {
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [
          { role: 'user', content: 'Hello, how are you?' },
          { role: 'assistant', content: 'I am doing well, thank you!' },
        ],
      } as any;

      const attributes = getAnthropicInputMessagesAttributes(params);

      expect(attributes).toEqual({
        'llm.input_messages.0.message.role': 'user',
        'llm.input_messages.0.message.content': 'Hello, how are you?',
        'llm.input_messages.1.message.role': 'assistant',
        'llm.input_messages.1.message.content': 'I am doing well, thank you!',
      });
    });

    it('should extract attributes from messages with system prompt', () => {
      const params = {
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        system: 'You are a helpful assistant.',
        messages: [
          { role: 'user', content: 'Hello!' },
        ],
      } as any;

      const attributes = getAnthropicInputMessagesAttributes(params);

      expect(attributes).toEqual({
        'llm.input_messages.0.message.role': 'system',
        'llm.input_messages.0.message.content': 'You are a helpful assistant.',
        'llm.input_messages.1.message.role': 'user',
        'llm.input_messages.1.message.content': 'Hello!',
      });
    });

    it('should handle empty messages array', () => {
      const params = {
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [],
      } as any;

      const attributes = getAnthropicInputMessagesAttributes(params);

      expect(Object.keys(attributes)).toHaveLength(0);
    });

    it('should handle complex content blocks', () => {
      const params = {
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [
          {
            role: 'user',
            content: [
              { type: 'text', text: 'What do you see in this image?' },
              {
                type: 'image',
                source: {
                  type: 'base64',
                  media_type: 'image/jpeg',
                  data: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
                },
              },
            ],
          },
        ],
      } as any;

      const attributes = getAnthropicInputMessagesAttributes(params);

      expect(attributes['llm.input_messages.0.message.role']).toBe('user');
      expect(attributes['llm.input_messages.0.message.contents.0.message_content.type']).toBe('text');
      expect(attributes['llm.input_messages.0.message.contents.0.message_content.text']).toBe('What do you see in this image?');
      expect(attributes['llm.input_messages.0.message.contents.1.message_content.type']).toBe('image');
      expect(attributes['llm.input_messages.0.message.contents.1.message_content.image']).toContain('base64');
    });

    it('should handle tool use content blocks', () => {
      const params = {
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [
          {
            role: 'assistant',
            content: [
              { type: 'text', text: 'I\'ll get the weather for you.' },
              {
                type: 'tool_use',
                id: 'toolu_123',
                name: 'get_weather',
                input: { location: 'San Francisco' },
              },
            ],
          },
        ],
      } as any;

      const attributes = getAnthropicInputMessagesAttributes(params);

      expect(attributes['llm.input_messages.0.message.role']).toBe('assistant');
      expect(attributes['llm.input_messages.0.message.contents.0.message_content.type']).toBe('text');
      expect(attributes['llm.input_messages.0.message.contents.0.message_content.text']).toBe('I\'ll get the weather for you.');
      expect(attributes['llm.input_messages.0.message.contents.1.message_content.type']).toBe('tool_use');
      expect(attributes['llm.input_messages.0.message.contents.1.message.content']).toContain('get_weather');
    });

    it('should handle tool result content blocks', () => {
      const params = {
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [
          {
            role: 'user',
            content: [
              {
                type: 'tool_result',
                tool_use_id: 'toolu_123',
                content: 'The weather in San Francisco is sunny and 72°F.',
              },
            ],
          },
        ],
      } as any;

      const attributes = getAnthropicInputMessagesAttributes(params);

      expect(attributes['llm.input_messages.0.message.role']).toBe('user');
      expect(attributes['llm.input_messages.0.message.contents.0.message_content.type']).toBe('tool_result');
      expect(attributes['llm.input_messages.0.message.contents.0.message.content']).toContain('tool_use_id');
    });

    it('should handle complex system prompt objects', () => {
      const systemPrompt = [
        { type: 'text', text: 'You are an assistant.' },
        { type: 'text', text: 'Be helpful and concise.' },
      ];
      
      const params = {
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        system: systemPrompt,
        messages: [
          { role: 'user', content: 'Hello!' },
        ],
      } as any;

      const attributes = getAnthropicInputMessagesAttributes(params);

      expect(attributes['llm.input_messages.0.message.role']).toBe('system');
      expect(attributes['llm.input_messages.0.message.content']).toContain('type');
      expect(attributes['llm.input_messages.1.message.role']).toBe('user');
    });
  });

  describe('getAnthropicOutputMessagesAttributes', () => {
    it('should extract attributes from text response', () => {
      const response = {
        id: 'msg_123',
        type: 'message',
        role: 'assistant',
        content: [
          { type: 'text', text: 'Hello! How can I help you today?' },
        ],
        model: 'claude-3-sonnet-20240229',
        stop_reason: 'end_turn',
        stop_sequence: null,
        usage: { input_tokens: 10, output_tokens: 15 },
      } as any;

      const attributes = getAnthropicOutputMessagesAttributes(response);

      expect(attributes).toEqual({
        'llm.output_messages.0.message.role': 'assistant',
        'llm.output_messages.0.message.content': 'Hello! How can I help you today?',
      });
    });

    it('should extract attributes from tool use response', () => {
      const response = {
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
        usage: { input_tokens: 20, output_tokens: 10 },
      } as any;

      const attributes = getAnthropicOutputMessagesAttributes(response);

      expect(attributes).toEqual({
        'llm.output_messages.0.message.role': 'assistant',
        'llm.output_messages.0.message.tool_calls.0.tool_call.function.name': 'get_weather',
        'llm.output_messages.0.message.tool_calls.0.tool_call.function.arguments': '{"location":"San Francisco"}',
      });
    });

    it('should extract attributes from mixed content response', () => {
      const response = {
        id: 'msg_123',
        type: 'message',
        role: 'assistant',
        content: [
          { type: 'text', text: 'I\'ll check the weather for you.' },
          {
            type: 'tool_use',
            id: 'toolu_123',
            name: 'get_weather',
            input: { location: 'New York' },
          },
          { type: 'text', text: 'Let me get that information.' },
        ],
        model: 'claude-3-sonnet-20240229',
        stop_reason: 'tool_use',
        stop_sequence: null,
        usage: { input_tokens: 25, output_tokens: 20 },
      } as any;

      const attributes = getAnthropicOutputMessagesAttributes(response);

      expect(attributes).toEqual({
        'llm.output_messages.0.message.role': 'assistant',
        'llm.output_messages.0.message.content': 'Let me get that information.',
        'llm.output_messages.0.message.tool_calls.0.tool_call.function.name': 'get_weather',
        'llm.output_messages.0.message.tool_calls.0.tool_call.function.arguments': '{"location":"New York"}',
      });
    });

    it('should handle multiple tool uses', () => {
      const response = {
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
          {
            type: 'tool_use',
            id: 'toolu_456',
            name: 'get_time',
            input: { timezone: 'PST' },
          },
        ],
        model: 'claude-3-sonnet-20240229',
        stop_reason: 'tool_use',
        stop_sequence: null,
        usage: { input_tokens: 30, output_tokens: 25 },
      } as any;

      const attributes = getAnthropicOutputMessagesAttributes(response);

      expect(attributes['llm.output_messages.0.message.role']).toBe('assistant');
      expect(attributes['llm.output_messages.0.message.tool_calls.0.tool_call.function.name']).toBe('get_weather');
      expect(attributes['llm.output_messages.0.message.tool_calls.1.tool_call.function.name']).toBe('get_time');
      expect(attributes['llm.output_messages.0.message.tool_calls.0.tool_call.function.arguments']).toBe('{"location":"San Francisco"}');
      expect(attributes['llm.output_messages.0.message.tool_calls.1.tool_call.function.arguments']).toBe('{"timezone":"PST"}');
    });

    it('should handle response with no content', () => {
      const response = {
        id: 'msg_123',
        type: 'message',
        role: 'assistant',
        content: [],
        model: 'claude-3-sonnet-20240229',
        stop_reason: 'end_turn',
        stop_sequence: null,
        usage: { input_tokens: 10, output_tokens: 0 },
      } as any;

      const attributes = getAnthropicOutputMessagesAttributes(response);

      expect(attributes).toEqual({
        'llm.output_messages.0.message.role': 'assistant',
      });
    });
  });

  describe('getAnthropicUsageAttributes', () => {
    it('should extract usage attributes from response', () => {
      const response = {
        id: 'msg_123',
        type: 'message',
        role: 'assistant',
        content: [{ type: 'text', text: 'Hello!' }],
        model: 'claude-3-sonnet-20240229',
        stop_reason: 'end_turn',
        stop_sequence: null,
        usage: { input_tokens: 10, output_tokens: 5 },
      } as any;

      const attributes = getAnthropicUsageAttributes(response);

      expect(attributes).toEqual({
        [SemanticConventions.LLM_TOKEN_COUNT_PROMPT]: 10,
        [SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]: 5,
        [SemanticConventions.LLM_TOKEN_COUNT_TOTAL]: 15,
      });
    });

    it('should handle object without usage property', () => {
      const response = {};

      const attributes = getAnthropicUsageAttributes(response);

      expect(attributes).toEqual({});
    });

    it('should handle zero usage values', () => {
      const response = {
        usage: { input_tokens: 0, output_tokens: 0 },
      };

      const attributes = getAnthropicUsageAttributes(response);

      expect(attributes).toEqual({
        [SemanticConventions.LLM_TOKEN_COUNT_PROMPT]: 0,
        [SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]: 0,
        [SemanticConventions.LLM_TOKEN_COUNT_TOTAL]: 0,
      });
    });
  });

  describe('getAnthropicToolsAttributes', () => {
    it('should extract attributes from tool definitions', () => {
      const params = {
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [{ role: 'user', content: 'What\'s the weather?' }],
        tools: [
          {
            name: 'get_weather',
            description: 'Get current weather information',
            input_schema: {
              type: 'object',
              properties: {
                location: { type: 'string', description: 'City name' },
              },
              required: ['location'],
            },
          },
        ],
      } as any;

      const attributes = getAnthropicToolsAttributes(params);

      expect(attributes['llm.tools.0.tool.name']).toBe('get_weather');
      expect(attributes['llm.tools.0.tool.description']).toBe('Get current weather information');
      expect(attributes['llm.tools.0.tool.json_schema']).toContain('location');
    });

    it('should handle params without tools', () => {
      const params = {
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [{ role: 'user', content: 'Hello' }],
      } as any;

      const attributes = getAnthropicToolsAttributes(params);

      expect(attributes).toEqual({});
    });

    it('should handle empty tools array', () => {
      const params = {
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [{ role: 'user', content: 'Hello' }],
        tools: [],
      } as any;

      const attributes = getAnthropicToolsAttributes(params);

      expect(attributes).toEqual({});
    });
  });

  describe('aggregateAnthropicStreamEvents', () => {
    it('should handle empty stream events', () => {
      const chunks: any[] = [];

      const result = aggregateAnthropicStreamEvents(chunks);

      expect(result.reconstructedMessage.id).toBe('');
      expect(result.reconstructedMessage.content).toEqual([]);
      expect(result.rawOutputChunks).toEqual([]);
    });

    it('should aggregate basic stream events', () => {
      const chunks = [
        {
          type: 'message_start',
          message: {
            id: 'msg_123',
            type: 'message',
            role: 'assistant',
            content: [],
            model: 'claude-3-sonnet-20240229',
            stop_reason: null,
            stop_sequence: null,
            usage: { input_tokens: 10, output_tokens: 0 },
          },
        },
        {
          type: 'message_stop',
        },
      ] as any;

      const result = aggregateAnthropicStreamEvents(chunks);

      expect(result.reconstructedMessage.id).toBe('msg_123');
      expect(result.reconstructedMessage.role).toBe('assistant');
      expect(result.rawOutputChunks).toEqual(chunks);
    });

    it('should handle tool use stream events', () => {
      const chunks = [
        {
          type: 'message_start',
          message: {
            id: 'msg_456',
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
            id: 'toolu_789',
            name: 'get_weather',
            input: {},
          },
        },
        {
          type: 'content_block_delta',
          index: 0,
          delta: { type: 'input_json_delta', partial_json: '{"location": "San Francisco"}' },
        },
        {
          type: 'content_block_stop',
          index: 0,
        },
        {
          type: 'message_delta',
          delta: { stop_reason: 'tool_use', stop_sequence: null },
          usage: { output_tokens: 5 },
        },
        {
          type: 'message_stop',
        },
      ] as any;

      const result = aggregateAnthropicStreamEvents(chunks);

      expect(result.reconstructedMessage.id).toBe('msg_456');
      expect(result.reconstructedMessage.content).toEqual([
        {
          type: 'tool_use',
          id: 'toolu_789',
          name: 'get_weather',
          input: { location: 'San Francisco' },
        },
      ]);
      expect(result.reconstructedMessage.stop_reason).toBe('tool_use');
    });

    it('should handle mixed content stream events', () => {
      const chunks = [
        {
          type: 'message_start',
          message: {
            id: 'msg_789',
            type: 'message',
            role: 'assistant',
            content: [],
            model: 'claude-3-sonnet-20240229',
            stop_reason: null,
            stop_sequence: null,
            usage: { input_tokens: 20, output_tokens: 0 },
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
          delta: { type: 'text_delta', text: 'I\'ll check the weather.' },
        },
        {
          type: 'content_block_stop',
          index: 0,
        },
        {
          type: 'content_block_start',
          index: 1,
          content_block: {
            type: 'tool_use',
            id: 'toolu_abc',
            name: 'get_weather',
            input: {},
          },
        },
        {
          type: 'content_block_delta',
          index: 1,
          delta: { type: 'input_json_delta', partial_json: '{"location": "NYC"}' },
        },
        {
          type: 'content_block_stop',
          index: 1,
        },
        {
          type: 'message_delta',
          delta: { stop_reason: 'tool_use', stop_sequence: null },
          usage: { output_tokens: 8 },
        },
        {
          type: 'message_stop',
        },
      ] as any;

      const result = aggregateAnthropicStreamEvents(chunks);

      expect(result.reconstructedMessage.content).toEqual([
        { type: 'text', text: 'I\'ll check the weather.' },
        {
          type: 'tool_use',
          id: 'toolu_abc',
          name: 'get_weather',
          input: { location: 'NYC' },
        },
      ]);
    });

    it('should handle invalid JSON in tool use', () => {
      const consoleSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
      
      const chunks = [
        {
          type: 'message_start',
          message: {
            id: 'msg_error',
            type: 'message',
            role: 'assistant',
            content: [],
            model: 'claude-3-sonnet-20240229',
            stop_reason: null,
            stop_sequence: null,
            usage: { input_tokens: 10, output_tokens: 0 },
          },
        },
        {
          type: 'content_block_start',
          index: 0,
          content_block: {
            type: 'tool_use',
            id: 'toolu_bad',
            name: 'bad_tool',
            input: {},
          },
        },
        {
          type: 'content_block_delta',
          index: 0,
          delta: { type: 'input_json_delta', partial_json: '{"invalid": json}' },
        },
        {
          type: 'content_block_stop',
          index: 0,
        },
        {
          type: 'message_stop',
        },
      ] as any;

      const result = aggregateAnthropicStreamEvents(chunks);

      expect(result.reconstructedMessage.content).toEqual([
        {
          type: 'tool_use',
          id: 'toolu_bad',
          name: 'bad_tool',
          input: {},
        },
      ]);
      expect(consoleSpy).toHaveBeenCalledWith(
        'Failed to parse tool_use input JSON from stream:',
        expect.any(Error)
      );
      
      consoleSpy.mockRestore();
    });
  });
}); 