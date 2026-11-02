import { Attributes } from '@opentelemetry/api';
import { ReadableSpan } from '@opentelemetry/sdk-trace-base';
import { addFIAttributesToSpan, safelyGetFIAttributes, isFISpan } from '../utils';

function normalize(attributes: Attributes) {
  const span = { attributes: { ...attributes } } as ReadableSpan;
  addFIAttributesToSpan(span);
  return span;
}

const llm = { 'operation.name': 'ai.generateText.doGenerate test' };

describe('AI SDK native span normalization', () => {
  test('keeps every tool result and current input fields in conversation history', () => {
    const span = normalize({ ...llm, 'ai.prompt.messages': JSON.stringify([
      { role: 'assistant', content: [
        { type: 'tool-call', toolCallId: 'call-1', toolName: 'weather', input: { city: 'Paris' } },
        { type: 'tool-call', toolCallId: 'call-2', toolName: 'count', input: false },
      ] },
      { role: 'tool', content: [
        { type: 'tool-result', toolCallId: 'call-1', output: { type: 'json', value: 0 } },
        { type: 'tool-result', toolCallId: 'call-2', output: { type: 'text', value: '' } },
      ] },
    ]) });
    expect(JSON.parse(span.attributes['gen_ai.input.messages'] as string)).toEqual([
      { role: 'assistant', tool_calls: [
        { id: 'call-1', function: { name: 'weather', arguments: '{"city":"Paris"}' } },
        { id: 'call-2', function: { name: 'count', arguments: 'false' } },
      ] },
      { role: 'tool', tool_call_id: 'call-1', content: '0' },
      { role: 'tool', tool_call_id: 'call-2', content: '' },
    ]);
  });

  test.each(['input', 'args'])('preserves response tool identifiers and %s arguments', (field) => {
    const attrs = safelyGetFIAttributes({ ...llm, 'ai.response.toolCalls': JSON.stringify([
      { toolCallId: 'call-1', toolName: 'weather', [field]: { city: 'Paris' } },
      { toolCallId: 'call-2', toolName: 'count', [field]: '{"count":0}' },
    ]) });
    expect(JSON.parse(attrs!['gen_ai.output.messages'] as string)).toEqual([
      { role: 'assistant', tool_calls: [
        { id: 'call-1', function: { name: 'weather', arguments: '{"city":"Paris"}' } },
        { id: 'call-2', function: { name: 'count', arguments: '{"count":0}' } },
      ] },
    ]);
  });

  test('retains legacy falsey tool results', () => {
    const span = normalize({ ...llm, 'ai.prompt.messages': JSON.stringify([
      { role: 'tool', content: [
        { toolCallId: 'zero', result: 0 },
        { toolCallId: 'false', result: false },
        { toolCallId: 'null', result: null },
        { toolCallId: 'object', result: { type: 'json', value: 42, unit: 'items' } },
      ] },
    ]) });
    expect(JSON.parse(span.attributes['gen_ai.input.messages'] as string)).toEqual([
      { role: 'tool', tool_call_id: 'zero', content: '0' },
      { role: 'tool', tool_call_id: 'false', content: 'false' },
      { role: 'tool', tool_call_id: 'null', content: 'null' },
      { role: 'tool', tool_call_id: 'object', content: '{"type":"json","value":42,"unit":"items"}' },
    ]);
  });

  test.each([
    ['invoke_agent', 'AGENT'], ['agent_step', 'CHAIN'], ['chat', 'LLM'],
    ['execute_tool', 'TOOL'], ['embeddings', 'EMBEDDING'],
    ['decide', 'EVALUATOR'], ['rerank', 'RERANKER'], ['ai.generateSpeech', 'LLM'], ['ai.transcribe', 'LLM'], ['ai.streamTranscribe', 'LLM'],
  ])('recognizes native GenAI %s spans without losing their attributes', (operation, kind) => {
    const attrs = { 'gen_ai.operation.name': operation, 'gen_ai.request.model': 'test-model',
      'gen_ai.input.messages': '[{"role":"user","parts":[{"type":"text","content":"hello"}]}]' };
    const span = normalize(attrs);
    expect(isFISpan(span)).toBe(true);
    expect(span.attributes).toEqual({ ...attrs, 'fi.span.kind': kind });
  });

  test.each(['ai.rerank', 'ai.rerank.doRerank', 'ai.rerank customer-function', 'ai.rerank.doRerank customer-function'])(
    'recognizes legacy SDK 6 %s spans', operation => {
      const span = normalize({ 'operation.name': operation });
      expect(isFISpan(span)).toBe(true);
      expect(span.attributes['fi.span.kind']).toBe('RERANKER');
    });

  test('does not alter unrelated spans or overwrite native GenAI messages', () => {
    expect(normalize({ 'http.request.method': 'GET' }).attributes).toStrictEqual({ 'http.request.method': 'GET' });
    const span = normalize({ ...llm, 'gen_ai.input.messages': 'native', 'ai.prompt.messages': '[]' });
    expect(span.attributes['gen_ai.input.messages']).toBe('native');
  });

  test('malformed messages do not break exports', () => {
    expect(() => normalize({ ...llm, 'ai.prompt.messages': 'not-json' })).not.toThrow();
    expect(normalize({ ...llm, 'ai.prompt.messages': 'null' }).attributes['gen_ai.input.messages']).toBeUndefined();
  });
});
