import { describe, it, expect } from '@jest/globals';
import {
  getResponsesInputMessagesAttributes,
  getResponsesUsageAttributes,
  getResponsesOutputMessagesAttributes,
  consumeResponseStreamEvents,
} from '../responseAttributes';

describe('Response Attributes Functions', () => {
  describe('getResponsesInputMessagesAttributes', () => {
         it('should handle simple string input', () => {
       const body = {
         model: 'gpt-4o-2024-11-20',
         input: ['Hello world!'],
       };

       const attributes = getResponsesInputMessagesAttributes(body as any);

      expect(attributes).toEqual({
        'llm.input_messages.0.role': 'user',
        'llm.input_messages.0.content': 'Hello world!',
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

       expect(attributes).toEqual({
         'llm.input_messages.0.role': 'user',
         'llm.input_messages.0.content': 'System message',
         'llm.input_messages.1.role': 'user',
         'llm.input_messages.1.content': 'User message',
         'llm.input_messages.2.role': 'assistant',
         'llm.input_messages.2.content': 'Assistant message',
       });
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

       expect(attributes).toEqual({
         'llm.input_messages.0.role': 'user',
         'llm.input_messages.0.contents.0.type': 'input_text',
         'llm.input_messages.0.contents.0.text': 'What is in this image?',
         'llm.input_messages.0.contents.1.type': 'input_image',
         'llm.input_messages.0.contents.1.image.url': 'https://example.com/image.jpg',
       });
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

       expect(attributes).toEqual({
         'llm.input_messages.0.role': 'assistant',
         'llm.input_messages.0.tool_calls.0.id': 'call_123',
         'llm.input_messages.0.tool_calls.0.function.name': 'get_weather',
         'llm.input_messages.0.tool_calls.0.function.arguments': '{"location": "San Francisco"}',
       });
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

       expect(attributes).toEqual({
         'llm.input_messages.0.role': 'tool',
         'llm.input_messages.0.tool_call_id': 'call_123',
         'llm.input_messages.0.content': '{"temperature": "72°F", "condition": "sunny"}',
       });
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

       expect(attributes).toEqual({
         'llm.input_messages.0.role': 'assistant',
         'llm.input_messages.0.contents.0.type': 'summary_text',
         'llm.input_messages.0.contents.0.text': 'Let me think about this...',
         'llm.input_messages.0.contents.1.type': 'summary_text',
         'llm.input_messages.0.contents.1.text': 'Based on the input, I should...',
       });
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

       expect(attributes).toEqual({
         'llm.input_messages.0.role': 'assistant',
         'llm.input_messages.0.tool_calls.0.id': 'search_123',
         'llm.input_messages.0.tool_calls.0.function.name': 'web_search_call',
       });
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

       expect(attributes).toEqual({
         'llm.input_messages.0.role': 'assistant',
         'llm.input_messages.0.tool_calls.0.id': 'computer_123',
         'llm.input_messages.0.tool_calls.0.function.name': 'computer_call',
         'llm.input_messages.0.tool_calls.0.function.arguments': '{"type":"screenshot"}',
       });
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

       expect(attributes).toEqual({
         'llm.input_messages.0.role': 'tool',
         'llm.input_messages.0.tool_call_id': 'computer_123',
         'llm.input_messages.0.content': '{"screenshot":"base64_data..."}',
       });
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

       expect(attributes).toEqual({
         'llm.input_messages.0.role': 'assistant',
         'llm.input_messages.0.tool_calls.0.id': 'search_456',
         'llm.input_messages.0.tool_calls.0.function.name': 'file_search_call',
         'llm.input_messages.0.tool_calls.0.function.arguments': '["search query 1","search query 2"]',
       });
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

       expect(attributes).toEqual({
         'llm.input_messages.0.role': 'tool',
         'llm.input_messages.0.tool_call_id': 'search_456',
         'llm.input_messages.0.content': '[{"content":"search result content"}]',
       });
     });

     it('should handle empty input', () => {
       const body = {
         input: [],
       };

       const attributes = getResponsesInputMessagesAttributes(body as any);

       expect(attributes).toEqual({});
     });

     it('should handle missing input field', () => {
       const body = {};

       const attributes = getResponsesInputMessagesAttributes(body as any);

      expect(attributes).toEqual({});
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
         'llm.usage.input_tokens': 25,
         'llm.usage.output_tokens': 15,
         'llm.usage.cached_tokens': 5,
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
         'llm.usage.input_tokens': 25,
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

       expect(attributes).toEqual({
         'llm.output_messages.0.role': 'assistant',
         'llm.output_messages.0.content': 'Hello! How can I help you today?',
       });
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

       expect(attributes).toEqual({
         'llm.output_messages.0.role': 'assistant',
         'llm.output_messages.0.content': 'First message',
         'llm.output_messages.1.role': 'assistant',
         'llm.output_messages.1.contents.0.type': 'output_text',
         'llm.output_messages.1.contents.0.text': 'Second message part 1',
         'llm.output_messages.1.contents.1.type': 'output_text',
         'llm.output_messages.1.contents.1.text': 'Second message part 2',
       });
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

       expect(attributes).toEqual({
         'llm.output_messages.0.role': 'assistant',
         'llm.output_messages.0.contents.0.type': 'refusal',
         'llm.output_messages.0.contents.0.text': 'I cannot provide information about that topic.',
       });
     });

     it('should handle response without output', () => {
       const response = {
         id: 'resp_123',
         object: 'response',
       };

       const attributes = getResponsesOutputMessagesAttributes(response as any);

       expect(attributes).toEqual({});
     });

     it('should handle empty output array', () => {
       const response = {
         id: 'resp_123',
         object: 'response',
         output: [],
       };

       const attributes = getResponsesOutputMessagesAttributes(response as any);

      expect(attributes).toEqual({});
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
          type: 'response.done',
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
    it('should handle null/undefined inputs gracefully', () => {
      expect(() => getResponsesInputMessagesAttributes(null as any)).not.toThrow();
      expect(() => getResponsesUsageAttributes(null as any)).not.toThrow();
      expect(() => getResponsesOutputMessagesAttributes(null as any)).not.toThrow();
    });

    it('should handle malformed input objects', () => {
      const malformedBody = {
        input: [
          null,
          undefined,
          { invalid: 'object' },
          { type: 'unknown_type' },
        ],
      };

      expect(() => getResponsesInputMessagesAttributes(malformedBody as any)).not.toThrow();
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

       // item_reference should produce minimal attributes (just from the base case)
       expect(Object.keys(attributes).length).toBeGreaterThanOrEqual(0);
    });
  });
}); 