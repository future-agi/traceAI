import { Attributes } from '@opentelemetry/api';
import { BasicTracerProvider, InMemorySpanExporter } from '@opentelemetry/sdk-trace-base';
import { FIBatchSpanProcessor, FISimpleSpanProcessor } from '../FISpanProcessor';
import { isFISpan } from '../utils';

const flags = ['INPUTS', 'OUTPUTS', 'INPUT_MESSAGES', 'OUTPUT_MESSAGES', 'INPUT_TEXT',
  'OUTPUT_TEXT', 'INPUT_IMAGES', 'EMBEDDING_VECTORS', 'ERROR_DETAILS'];
const originalEnv = Object.fromEntries(flags.map(flag => [`FI_HIDE_${flag}`, process.env[`FI_HIDE_${flag}`]]));
beforeEach(() => { for (const flag of flags) delete process.env[`FI_HIDE_${flag}`]; });
afterEach(() => {
  for (const [key, value] of Object.entries(originalEnv)) {
    if (value === undefined) delete process.env[key]; else process.env[key] = value;
  }
});

// Every content entry, with an indexed child that source masking would not have touched.
const INPUT_ENTRIES = [
  'input.value', 'input.mime_type', 'raw.input', 'ai.prompt', 'ai.prompt.messages', 'ai.prompt.tools',
  'gen_ai.input.messages', 'gen_ai.system_instructions', 'gen_ai.tool.call.arguments',
  'gen_ai.tool.definitions', 'ai.toolCall.args', 'tool.parameters',
  'ai.value', 'ai.values', 'embedding.text', 'ai.documents', 'ai.request.text',
  'reranker.query', 'reranker.input_documents', 'llm.prompt_template.variables',
  'gen_ai.prompt.template.variables', 'ai.schema', 'ai.schema.description',
  'ai.decision.state', 'ai.decision.questions',
];
const OUTPUT_ENTRIES = [
  'output.value', 'output.mime_type', 'raw.output', 'gen_ai.output.messages',
  'ai.response.text', 'ai.response.object', 'ai.response.toolCalls',
  'ai.response.reasoning', 'ai.toolCall.result', 'gen_ai.tool.call.result',
  'message.tool_calls', 'reranker.output_documents', 'ai.decision.answers', 'ai.ranking',
];

const childrenOf = (entries: string[]): Attributes => {
  const attributes: Attributes = {};
  for (const entry of entries) attributes[`${entry}.0.child.json_schema`] = 'private-content';
  return attributes;
};

for (const Processor of [FISimpleSpanProcessor, FIBatchSpanProcessor]) {
  describe(`${Processor.name} indexed content keys`, () => {
    async function capture(attributes: Attributes) {
      const exporter = new InMemorySpanExporter();
      const provider = new BasicTracerProvider();
      provider.addSpanProcessor(new Processor({ exporter, spanFilter: isFISpan }));
      const span = provider.getTracer('privacy').startSpan('native', { attributes: {
        'gen_ai.operation.name': 'chat', 'gen_ai.request.model': 'safe-model', ...attributes,
      } });
      span.end();
      await provider.forceFlush();
      const finished = exporter.getFinishedSpans().slice();
      await provider.shutdown();
      expect(finished).toHaveLength(1);
      return finished[0].attributes;
    }

    test.each(['INPUTS', 'INPUT_MESSAGES', 'INPUT_TEXT'])(
      'FI_HIDE_%s drops indexed children of every input entry', async flag => {
        process.env[`FI_HIDE_${flag}`] = 'true';
        const exported = await capture({
          ...childrenOf(INPUT_ENTRIES),
          'gen_ai.tool.definitions.0.tool.json_schema': 'private-content',
          'gen_ai.tool.definitions.1.tool.name': 'private-content',
        });
        expect(JSON.stringify(exported)).not.toContain('private-content');
        expect(exported['gen_ai.request.model']).toBe('safe-model');
      });

    test.each(['OUTPUTS', 'OUTPUT_MESSAGES', 'OUTPUT_TEXT'])(
      'FI_HIDE_%s drops indexed children of every output entry', async flag => {
        process.env[`FI_HIDE_${flag}`] = 'true';
        const exported = await capture(childrenOf(OUTPUT_ENTRIES));
        expect(JSON.stringify(exported)).not.toContain('private-content');
      });

    test('FI_HIDE_EMBEDDING_VECTORS drops indexed vector children', async () => {
      process.env.FI_HIDE_EMBEDDING_VECTORS = 'true';
      const exported = await capture({
        'ai.embedding.0': 'private-content', 'ai.embeddings.0': 'private-content',
        'embedding.vector.0': 'private-content',
      });
      expect(JSON.stringify(exported)).not.toContain('private-content');
    });

    test('keeps indexed children when no hide setting is on', async () => {
      const exported = await capture({
        ...childrenOf([...INPUT_ENTRIES, ...OUTPUT_ENTRIES]),
        'gen_ai.tool.definitions.0.tool.json_schema': 'private-content',
      });
      expect(exported['gen_ai.tool.definitions.0.tool.json_schema']).toBe('private-content');
      expect(exported['ai.prompt.0.child.json_schema']).toBe('private-content');
      expect(exported['ai.response.text.0.child.json_schema']).toBe('private-content');
    });

    test('does not drop sibling keys that merely share a name prefix', async () => {
      process.env.FI_HIDE_INPUTS = 'true';
      const exported = await capture({ 'ai.promptness': 'public', 'ai.valued': 'public' });
      expect(exported['ai.promptness']).toBe('public');
      expect(exported['ai.valued']).toBe('public');
    });
  });
}
