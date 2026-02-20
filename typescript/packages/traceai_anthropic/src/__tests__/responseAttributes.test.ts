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
    it('should extract attributes from simple text messages as a JSON blob', () => {
      const params = {
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [
          { role: 'user', content: 'Hello, how are you?' },
          { role: 'assistant', content: 'I am doing well, thank you!' },
        ],
      } as any;

      const attributes = getAnthropicInputMessagesAttributes(params);

      expect(Object.keys(attributes)).toHaveLength(1);
      expect(attributes).toHaveProperty([SemanticConventions.LLM_INPUT_MESSAGES]);

      const parsed = JSON.parse(attributes[SemanticConventions.LLM_INPUT_MESSAGES] as string);
      expect(parsed).toHaveLength(2);
      expect(parsed[0]).toEqual({ role: 'user', content: 'Hello, how are you?' });
      expect(parsed[1]).toEqual({ role: 'assistant', content: 'I am doing well, thank you!' });
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

      const parsed = JSON.parse(attributes[SemanticConventions.LLM_INPUT_MESSAGES] as string);
      expect(parsed).toHaveLength(2);
      expect(parsed[0]).toEqual({ role: 'system', content: 'You are a helpful assistant.' });
      expect(parsed[1]).toEqual({ role: 'user', content: 'Hello!' });
    });

    it('should handle empty messages array', () => {
      const params = {
        model: 'claude-3-sonnet-20240229',
        max_tokens: 1000,
        messages: [],
      } as any;

      const attributes = getAnthropicInputMessagesAttributes(params);

      const parsed = JSON.parse(attributes[SemanticConventions.LLM_INPUT_MESSAGES] as string);
      expect(parsed).toHaveLength(0);
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

      const parsed = JSON.parse(attributes[SemanticConventions.LLM_INPUT_MESSAGES] as string);
      expect(parsed).toHaveLength(1);
      expect(parsed[0].role).toBe('user');
      expect(parsed[0].content).toHaveLength(2);
      expect(parsed[0].content[0]).toEqual({ type: 'text', text: 'What do you see in this image?' });
      expect(parsed[0].content[1].type).toBe('image');
      expect(parsed[0].content[1].source.type).toBe('base64');
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

      const parsed = JSON.parse(attributes[SemanticConventions.LLM_INPUT_MESSAGES] as string);
      expect(parsed).toHaveLength(1);
      expect(parsed[0].role).toBe('assistant');
      expect(parsed[0].content).toHaveLength(2);
      expect(parsed[0].content[0]).toEqual({ type: 'text', text: 'I\'ll get the weather for you.' });
      expect(parsed[0].content[1].type).toBe('tool_use');
      expect(parsed[0].content[1].id).toBe('toolu_123');
      expect(parsed[0].content[1].name).toBe('get_weather');
      expect(parsed[0].content[1].input).toEqual({ location: 'San Francisco' });
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
                content: 'The weather in San Francisco is sunny and 72\u00b0F.',
              },
            ],
          },
        ],
      } as any;

      const attributes = getAnthropicInputMessagesAttributes(params);

      const parsed = JSON.parse(attributes[SemanticConventions.LLM_INPUT_MESSAGES] as string);
      expect(parsed).toHaveLength(1);
      expect(parsed[0].role).toBe('user');
      expect(parsed[0].content).toHaveLength(1);
      expect(parsed[0].content[0].type).toBe('tool_result');
      expect(parsed[0].content[0].tool_use_id).toBe('toolu_123');
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

      const parsed = JSON.parse(attributes[SemanticConventions.LLM_INPUT_MESSAGES] as string);
      expect(parsed).toHaveLength(2);
      expect(parsed[0].role).toBe('system');
      expect(parsed[0].content).toEqual(systemPrompt);
      expect(parsed[1]).toEqual({ role: 'user', content: 'Hello!' });
    });
  });

  describe('getAnthropicOutputMessagesAttributes', () => {
    it('should extract attributes from text response as a JSON blob', () => {
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

      expect(Object.keys(attributes)).toHaveLength(1);
      expect(attributes).toHaveProperty([SemanticConventions.LLM_OUTPUT_MESSAGES]);

      const parsed = JSON.parse(attributes[SemanticConventions.LLM_OUTPUT_MESSAGES] as string);
      expect(parsed).toHaveLength(1);
      expect(parsed[0].role).toBe('assistant');
      expect(parsed[0].content).toBe('Hello! How can I help you today?');
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

      const parsed = JSON.parse(attributes[SemanticConventions.LLM_OUTPUT_MESSAGES] as string);
      expect(parsed).toHaveLength(1);
      expect(parsed[0].role).toBe('assistant');
      expect(parsed[0].tool_calls).toHaveLength(1);
      expect(parsed[0].tool_calls[0].function.name).toBe('get_weather');
      expect(parsed[0].tool_calls[0].function.arguments).toBe('{"location":"San Francisco"}');
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

      const parsed = JSON.parse(attributes[SemanticConventions.LLM_OUTPUT_MESSAGES] as string);
      expect(parsed).toHaveLength(1);
      expect(parsed[0].role).toBe('assistant');
      // The implementation keeps the *last* text content
      expect(parsed[0].content).toBe('Let me get that information.');
      expect(parsed[0].tool_calls).toHaveLength(1);
      expect(parsed[0].tool_calls[0].function.name).toBe('get_weather');
      expect(parsed[0].tool_calls[0].function.arguments).toBe('{"location":"New York"}');
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

      const parsed = JSON.parse(attributes[SemanticConventions.LLM_OUTPUT_MESSAGES] as string);
      expect(parsed).toHaveLength(1);
      expect(parsed[0].role).toBe('assistant');
      expect(parsed[0].tool_calls).toHaveLength(2);
      expect(parsed[0].tool_calls[0].function.name).toBe('get_weather');
      expect(parsed[0].tool_calls[0].function.arguments).toBe('{"location":"San Francisco"}');
      expect(parsed[0].tool_calls[1].function.name).toBe('get_time');
      expect(parsed[0].tool_calls[1].function.arguments).toBe('{"timezone":"PST"}');
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

      const parsed = JSON.parse(attributes[SemanticConventions.LLM_OUTPUT_MESSAGES] as string);
      expect(parsed).toHaveLength(1);
      expect(parsed[0].role).toBe('assistant');
      expect(parsed[0].content).toBeUndefined();
      expect(parsed[0].tool_calls).toBeUndefined();
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
    it('should extract tool definitions as a JSON blob', () => {
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

      expect(Object.keys(attributes)).toHaveLength(1);
      expect(attributes).toHaveProperty([SemanticConventions.LLM_TOOLS]);

      const parsed = JSON.parse(attributes[SemanticConventions.LLM_TOOLS] as string);
      expect(parsed).toHaveLength(1);
      expect(parsed[0].name).toBe('get_weather');
      expect(parsed[0].description).toBe('Get current weather information');
      expect(parsed[0].input_schema.properties.location.type).toBe('string');
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

      // Empty array is truthy, so the implementation still serializes it
      const parsed = JSON.parse(attributes[SemanticConventions.LLM_TOOLS] as string);
      expect(parsed).toEqual([]);
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
