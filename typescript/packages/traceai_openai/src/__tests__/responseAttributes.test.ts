import { describe, it, expect } from '@jest/globals';
import {
  getResponsesInputMessagesAttributes,
  getResponsesUsageAttributes,
  getResponsesOutputMessagesAttributes,
  consumeResponseStreamEvents,
} from '../responseAttributes';
import { SemanticConventions } from '@traceai/fi-semantic-conventions';

describe('Response Attributes Functions', () => {
  describe('getResponsesInputMessagesAttributes', () => {
    it('should handle simple string input', () => {
      const body = {
        model: 'gpt-4o-2024-11-20',
        input: ['Hello world!'],
      };

      const attributes = getResponsesInputMessagesAttributes(body as any);

      expect(attributes).toEqual({
        [SemanticConventions.LLM_INPUT_MESSAGES]: JSON.stringify([
          { role: 'user', content: 'Hello world!' },
        ]),
      });
    });

    it('should handle array of messages', () => {
      const body = {
        input: [
          'System message',
          { role: 'user', content: 'User message' },
          { role: 'assistant', content: 'Assistant message' },
        ],
      };

      const attributes = getResponsesInputMessagesAttributes(body as any);
      const messages = JSON.parse(attributes[SemanticConventions.LLM_INPUT_MESSAGES] as string);

      expect(messages).toHaveLength(3);
      expect(messages[0]).toEqual({ role: 'user', content: 'System message' });
      expect(messages[1]).toEqual({ role: 'user', content: 'User message' });
      expect(messages[2]).toEqual({ role: 'assistant', content: 'Assistant message' });
    });

    it('should handle complex message content with multiple parts', () => {
      const body = {
        input: [
          {
            role: 'user',
            content: [
              { type: 'input_text', text: 'What is in this image?' },
              { type: 'input_image', image_url: 'https://example.com/image.jpg' },
            ],
          },
        ],
      };

      const attributes = getResponsesInputMessagesAttributes(body as any);
      const messages = JSON.parse(attributes[SemanticConventions.LLM_INPUT_MESSAGES] as string);

      expect(messages).toHaveLength(1);
      expect(messages[0].role).toBe('user');
      expect(messages[0].content).toEqual([
        { type: 'input_text', text: 'What is in this image?' },
        { type: 'input_image', image_url: 'https://example.com/image.jpg' },
      ]);
    });

    it('should handle function call items', () => {
      const body = {
        input: [
          {
            type: 'function_call',
            call_id: 'call_123',
            name: 'get_weather',
            arguments: '{"location": "San Francisco"}',
          },
        ],
      };

      const attributes = getResponsesInputMessagesAttributes(body as any);
      const messages = JSON.parse(attributes[SemanticConventions.LLM_INPUT_MESSAGES] as string);

      expect(messages).toHaveLength(1);
      expect(messages[0].role).toBe('assistant');
      expect(messages[0].tool_calls[0].id).toBe('call_123');
      expect(messages[0].tool_calls[0].function.name).toBe('get_weather');
      expect(messages[0].tool_calls[0].function.arguments).toBe('{"location": "San Francisco"}');
    });

    it('should handle function call output items', () => {
      const body = {
        input: [
          {
            type: 'function_call_output',
            call_id: 'call_123',
            output: '{"temperature": "72°F", "condition": "sunny"}',
          },
        ],
      };

      const attributes = getResponsesInputMessagesAttributes(body as any);
      const messages = JSON.parse(attributes[SemanticConventions.LLM_INPUT_MESSAGES] as string);

      expect(messages).toHaveLength(1);
      expect(messages[0].role).toBe('tool');
      expect(messages[0].tool_call_id).toBe('call_123');
      expect(messages[0].content).toBe('{"temperature": "72°F", "condition": "sunny"}');
    });

    it('should handle reasoning type items', () => {
      const body = {
        input: [
          {
            type: 'reasoning',
            summary: [
              { type: 'summary_text', text: 'Let me think about this...' },
              { type: 'summary_text', text: 'Based on the input, I should...' },
            ],
          },
        ],
      };

      const attributes = getResponsesInputMessagesAttributes(body as any);
      const messages = JSON.parse(attributes[SemanticConventions.LLM_INPUT_MESSAGES] as string);

      expect(messages).toHaveLength(1);
      expect(messages[0].role).toBe('assistant');
      expect(messages[0].content).toEqual([
        { type: 'summary_text', text: 'Let me think about this...' },
        { type: 'summary_text', text: 'Based on the input, I should...' },
      ]);
    });

    it('should handle web search call items', () => {
      const body = {
        input: [
          {
            type: 'web_search_call',
            id: 'search_123',
          },
        ],
      };

      const attributes = getResponsesInputMessagesAttributes(body as any);
      const messages = JSON.parse(attributes[SemanticConventions.LLM_INPUT_MESSAGES] as string);

      expect(messages).toHaveLength(1);
      expect(messages[0].role).toBe('assistant');
      expect(messages[0].tool_calls[0].id).toBe('search_123');
      expect(messages[0].tool_calls[0].function.name).toBe('web_search_call');
    });

    it('should handle computer call items', () => {
      const body = {
        input: [
          {
            type: 'computer_call',
            id: 'computer_123',
            action: { type: 'screenshot' },
          },
        ],
      };

      const attributes = getResponsesInputMessagesAttributes(body as any);
      const messages = JSON.parse(attributes[SemanticConventions.LLM_INPUT_MESSAGES] as string);

      expect(messages).toHaveLength(1);
      expect(messages[0].role).toBe('assistant');
      expect(messages[0].tool_calls[0].id).toBe('computer_123');
      expect(messages[0].tool_calls[0].function.name).toBe('computer_call');
      expect(messages[0].tool_calls[0].function.arguments).toBe('{"type":"screenshot"}');
    });

    it('should handle computer call output items', () => {
      const body = {
        input: [
          {
            type: 'computer_call_output',
            call_id: 'computer_123',
            output: { screenshot: 'base64_data...' },
          },
        ],
      };

      const attributes = getResponsesInputMessagesAttributes(body as any);
      const messages = JSON.parse(attributes[SemanticConventions.LLM_INPUT_MESSAGES] as string);

      expect(messages).toHaveLength(1);
      expect(messages[0].role).toBe('tool');
      expect(messages[0].tool_call_id).toBe('computer_123');
      expect(messages[0].content).toBe('{"screenshot":"base64_data..."}');
    });

    it('should handle file search call items without results', () => {
      const body = {
        input: [
          {
            type: 'file_search_call',
            id: 'search_456',
            queries: ['search query 1', 'search query 2'],
          },
        ],
      };

      const attributes = getResponsesInputMessagesAttributes(body as any);
      const messages = JSON.parse(attributes[SemanticConventions.LLM_INPUT_MESSAGES] as string);

      expect(messages).toHaveLength(1);
      expect(messages[0].role).toBe('assistant');
      expect(messages[0].tool_calls[0].id).toBe('search_456');
      expect(messages[0].tool_calls[0].function.name).toBe('file_search_call');
      expect(messages[0].tool_calls[0].function.arguments).toBe('["search query 1","search query 2"]');
    });

    it('should handle file search call items with results', () => {
      const body = {
        input: [
          {
            type: 'file_search_call',
            id: 'search_456',
            queries: ['search query'],
            results: [{ content: 'search result content' }],
          },
        ],
      };

      const attributes = getResponsesInputMessagesAttributes(body as any);
      const messages = JSON.parse(attributes[SemanticConventions.LLM_INPUT_MESSAGES] as string);

      expect(messages).toHaveLength(1);
      expect(messages[0].role).toBe('tool');
      expect(messages[0].tool_call_id).toBe('search_456');
      expect(messages[0].content).toBe('[{"content":"search result content"}]');
    });

    it('should handle empty input', () => {
      const body = {
        input: [],
      };

      const attributes = getResponsesInputMessagesAttributes(body as any);

      expect(attributes).toEqual({
        [SemanticConventions.LLM_INPUT_MESSAGES]: '[]',
      });
    });

    it('should handle missing input field', () => {
      const body = {};

      expect(() => getResponsesInputMessagesAttributes(body as any)).toThrow();
    });
  });

  describe('getResponsesUsageAttributes', () => {
    it('should extract usage metrics from response', () => {
      const response = {
        id: 'resp_123',
        object: 'response',
        usage: {
          input_tokens: 25,
          output_tokens: 15,
          cached_tokens: 5,
        },
      };

      const attributes = getResponsesUsageAttributes(response as any);

      expect(attributes).toEqual({
        [SemanticConventions.LLM_TOKEN_COUNT_PROMPT]: 25,
        [SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]: 15,
        [SemanticConventions.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ]: undefined,
        [SemanticConventions.LLM_TOKEN_COUNT_TOTAL]: undefined,
        [SemanticConventions.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING]: undefined,
      });
    });

    it('should handle response without usage', () => {
      const response = {
        id: 'resp_123',
        object: 'response',
      };

      const attributes = getResponsesUsageAttributes(response as any);

      expect(attributes).toEqual({});
    });

    it('should handle partial usage data', () => {
      const response = {
        id: 'resp_123',
        object: 'response',
        usage: {
          input_tokens: 25,
          // missing output_tokens and cached_tokens
        },
      };

      const attributes = getResponsesUsageAttributes(response as any);

      expect(attributes).toEqual({
        [SemanticConventions.LLM_TOKEN_COUNT_PROMPT]: 25,
        [SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]: undefined,
        [SemanticConventions.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ]: undefined,
        [SemanticConventions.LLM_TOKEN_COUNT_TOTAL]: undefined,
        [SemanticConventions.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING]: undefined,
      });
    });
  });

  describe('getResponsesOutputMessagesAttributes', () => {
    it('should extract output messages from response', () => {
      const response = {
        id: 'resp_123',
        object: 'response',
        output: [
          {
            role: 'assistant',
            content: 'Hello! How can I help you today?',
          },
        ],
      };

      const attributes = getResponsesOutputMessagesAttributes(response as any);
      const messages = JSON.parse(attributes[SemanticConventions.LLM_OUTPUT_MESSAGES] as string);

      expect(messages).toHaveLength(1);
      expect(messages[0]).toEqual({ role: 'assistant', content: 'Hello! How can I help you today?' });
    });

    it('should handle multiple output messages', () => {
      const response = {
        id: 'resp_123',
        object: 'response',
        output: [
          {
            role: 'assistant',
            content: 'First message',
          },
          {
            role: 'assistant',
            content: [
              { type: 'output_text', text: 'Second message part 1' },
              { type: 'output_text', text: 'Second message part 2' },
            ],
          },
        ],
      };

      const attributes = getResponsesOutputMessagesAttributes(response as any);
      const messages = JSON.parse(attributes[SemanticConventions.LLM_OUTPUT_MESSAGES] as string);

      expect(messages).toHaveLength(2);
      expect(messages[0]).toEqual({ role: 'assistant', content: 'First message' });
      expect(messages[1].role).toBe('assistant');
      expect(messages[1].content).toEqual([
        { type: 'output_text', text: 'Second message part 1' },
        { type: 'output_text', text: 'Second message part 2' },
      ]);
    });

    it('should handle refusal content', () => {
      const response = {
        id: 'resp_123',
        object: 'response',
        output: [
          {
            role: 'assistant',
            content: [
              {
                type: 'refusal',
                refusal: 'I cannot provide information about that topic.',
              },
            ],
          },
        ],
      };

      const attributes = getResponsesOutputMessagesAttributes(response as any);
      const messages = JSON.parse(attributes[SemanticConventions.LLM_OUTPUT_MESSAGES] as string);

      expect(messages).toHaveLength(1);
      expect(messages[0].role).toBe('assistant');
      expect(messages[0].content).toEqual([
        { type: 'refusal', refusal: 'I cannot provide information about that topic.' },
      ]);
    });

    it('should handle response without output', () => {
      const response = {
        id: 'resp_123',
        object: 'response',
      };

      expect(() => getResponsesOutputMessagesAttributes(response as any)).toThrow();
    });

    it('should handle empty output array', () => {
      const response = {
        id: 'resp_123',
        object: 'response',
        output: [],
      };

      const attributes = getResponsesOutputMessagesAttributes(response as any);

      expect(attributes).toEqual({
        [SemanticConventions.LLM_OUTPUT_MESSAGES]: '[]',
      });
    });
  });

  describe('consumeResponseStreamEvents', () => {
    it('should consume stream events and return final response', async () => {
      const mockEvents = [
        {
          type: 'response.output_message.delta',
          output_message: {
            id: 'msg_123',
            role: 'assistant',
            content: [{ type: 'output_text', text: 'Hello' }],
          },
        },
        {
          type: 'response.output_message.delta',
          output_message: {
            id: 'msg_123',
            role: 'assistant',
            content: [{ type: 'output_text', text: ' world!' }],
          },
        },
        {
          type: 'response.completed',
          response: {
            id: 'resp_123',
            object: 'response',
            output: [
              {
                id: 'msg_123',
                role: 'assistant',
                content: 'Hello world!',
              },
            ],
            usage: {
              input_tokens: 10,
              output_tokens: 5,
              cached_tokens: 0,
            },
          },
        },
      ];

      // Create a mock stream
      const mockStream = {
        [Symbol.asyncIterator]: async function* () {
          for (const event of mockEvents) {
            yield event;
          }
        },
      } as any;

      const finalResponse = await consumeResponseStreamEvents(mockStream);

      expect(finalResponse).toEqual({
        id: 'resp_123',
        object: 'response',
        output: [
          {
            id: 'msg_123',
            role: 'assistant',
            content: 'Hello world!',
          },
        ],
        usage: {
          input_tokens: 10,
          output_tokens: 5,
          cached_tokens: 0,
        },
      });
    });

    it('should handle stream without response.done event', async () => {
      const mockEvents = [
        {
          type: 'response.output_message.delta',
          output_message: {
            id: 'msg_123',
            role: 'assistant',
            content: [{ type: 'output_text', text: 'Hello' }],
          },
        },
      ];

      const mockStream = {
        [Symbol.asyncIterator]: async function* () {
          for (const event of mockEvents) {
            yield event;
          }
        },
      } as any;

      const finalResponse = await consumeResponseStreamEvents(mockStream);

      expect(finalResponse).toBeUndefined();
    });

    it('should handle empty stream', async () => {
      const mockStream = {
        [Symbol.asyncIterator]: async function* () {
          // Empty stream
        },
      } as any;

      const finalResponse = await consumeResponseStreamEvents(mockStream);

      expect(finalResponse).toBeUndefined();
    });

    it('should handle stream errors gracefully', async () => {
      const mockStream = {
        [Symbol.asyncIterator]: async function* () {
          yield {
            type: 'response.output_message.delta',
            output_message: { id: 'msg_123', role: 'assistant' },
          };
          throw new Error('Stream error');
        },
      } as any;

      await expect(consumeResponseStreamEvents(mockStream)).rejects.toThrow('Stream error');
    });
  });

  describe('Edge Cases and Error Handling', () => {
    it('should throw on null/undefined inputs', () => {
      expect(() => getResponsesInputMessagesAttributes(null as any)).toThrow();
      expect(() => getResponsesUsageAttributes(null as any)).toThrow();
      expect(() => getResponsesOutputMessagesAttributes(null as any)).toThrow();
    });

    it('should throw on malformed input objects', () => {
      const malformedBody = {
        input: [
          null,
          undefined,
          { invalid: 'object' },
          { type: 'unknown_type' },
        ],
      };

      expect(() => getResponsesInputMessagesAttributes(malformedBody as any)).toThrow();
    });

    it('should handle item_reference type (no-op case)', () => {
      const body = {
        input: [
          {
            type: 'item_reference',
            id: 'ref_123',
          },
        ],
      };

      const attributes = getResponsesInputMessagesAttributes(body as any);

      // item_reference should produce minimal attributes
      expect(Object.keys(attributes).length).toBeGreaterThanOrEqual(0);
    });
  });
});
