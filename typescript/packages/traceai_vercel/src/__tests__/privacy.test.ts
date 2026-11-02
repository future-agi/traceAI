import { Attributes, SpanStatusCode } from '@opentelemetry/api';
import { BasicTracerProvider, InMemorySpanExporter, ReadableSpan } from '@opentelemetry/sdk-trace-base';
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

for (const Processor of [FISimpleSpanProcessor, FIBatchSpanProcessor]) {
  describe(Processor.name, () => {
    async function capture(attributes: Attributes, error = false) {
      const exporter = new InMemorySpanExporter();
      const provider = new BasicTracerProvider();
      provider.addSpanProcessor(new Processor({ exporter, spanFilter: isFISpan }));
      const span = provider.getTracer('privacy').startSpan('native', { attributes: {
        'gen_ai.operation.name': 'chat', 'gen_ai.request.model': 'safe-model', ...attributes,
      } });
      if (error) {
        span.recordException(new Error('private-error'));
        span.addEvent('provider-error', { 'provider.error.message': 'private-error', 'safe': 1 });
        span.setStatus({ code: SpanStatusCode.ERROR, message: 'private-error' });
      }
      span.end();
      await provider.forceFlush();
      const finished = exporter.getFinishedSpans().slice();
      await provider.shutdown();
      expect(finished).toHaveLength(1);
      expect(finished[0].attributes['gen_ai.request.model']).toBe('safe-model');
      return { exported: finished[0], original: span as unknown as ReadableSpan };
    }

    test.each(['INPUTS', 'INPUT_MESSAGES', 'INPUT_TEXT', 'INPUT_IMAGES'])(
      'FI_HIDE_%s masks native and normalized input copies', async flag => {
        process.env[`FI_HIDE_${flag}`] = 'true';
        const { exported } = await capture({
          'operation.name': 'ai.generateText.doGenerate',
          'ai.prompt': '{"prompt":"private-input"}',
          'ai.prompt.messages': '[{"role":"user","content":"private-input"}]',
          'ai.prompt.tools': ['{"description":"private-input","parameters":{"enum":["private-input"]}}'],
          'gen_ai.tool.definitions': '[{"description":"private-input"}]',
          'gen_ai.system_instructions': '[{"type":"text","content":"private-input"}]',
          'gen_ai.input.messages': '[{"role":"user","parts":[{"type":"blob","content":"private-input"}]}]',
          'input.value': 'private-input', 'raw.input': 'private-input',
        });
        expect(JSON.stringify(exported.attributes)).not.toContain('private-input');
      });

    test.each(['OUTPUTS', 'OUTPUT_MESSAGES', 'OUTPUT_TEXT'])(
      'FI_HIDE_%s masks native and normalized output copies', async flag => {
        process.env[`FI_HIDE_${flag}`] = 'true';
        const { exported } = await capture({
          'operation.name': 'ai.generateText.doGenerate',
          'ai.response.text': 'private-output', 'ai.response.object': '{"secret":"private-output"}',
          'ai.response.toolCalls': '[{"toolName":"lookup","input":{"secret":"private-output"}}]',
          'gen_ai.output.messages': '[{"role":"assistant","parts":[{"type":"text","content":"private-output"}]}]',
          'output.value': 'private-output', 'raw.output': 'private-output',
        });
        expect(JSON.stringify(exported.attributes)).not.toContain('private-output');
      });

    test('hides tool and embedding content, system instructions and vectors', async () => {
      process.env.FI_HIDE_INPUTS = 'true'; process.env.FI_HIDE_OUTPUTS = 'true';
      process.env.FI_HIDE_EMBEDDING_VECTORS = 'true';
      const { exported } = await capture({
        'ai.toolCall.args': '{"secret":"private-input"}', 'gen_ai.tool.call.arguments': 'private-input',
        'tool.parameters': 'private-input', 'ai.toolCall.result': 'private-output',
        'gen_ai.tool.call.result': 'private-output', 'ai.value': 'private-input', 'ai.values': ['private-input'],
        'embedding.text': 'private-input', 'ai.embedding': '[0.123456]', 'ai.embeddings': ['[0.123456]'],
        'embedding.embeddings.0.embedding.vector': [0.123456], 'embedding.vector': [0.123456],
        'ai.documents': '["private-input"]', 'ai.request.text': 'private-input',
        'ai.decision.state': 'private-input', 'ai.decision.questions': 'private-input',
        'ai.decision.answers': 'private-output',
      });
      expect(JSON.stringify(exported.attributes)).not.toMatch(/private-|0\.123456/);
    });

    test('hides vectors independently without removing embedding inputs', async () => {
      process.env.FI_HIDE_EMBEDDING_VECTORS = 'true';
      const { exported } = await capture({ 'ai.value': 'keep-input', 'ai.embedding': '[0.123456]', 'embedding.vector': [0.123456] });
      expect(JSON.stringify(exported.attributes)).not.toContain('0.123456');
      expect(exported.attributes['ai.value']).toBe('keep-input');
    });

    test.each(['INPUTS', 'OUTPUTS', 'INPUT_MESSAGES', 'OUTPUT_MESSAGES', 'INPUT_TEXT', 'OUTPUT_TEXT', 'ERROR_DETAILS'])(
      'FI_HIDE_%s suppresses error details but retains status and exception type', async flag => {
        process.env[`FI_HIDE_${flag}`] = 'true';
        const { exported } = await capture({ 'error.message': 'private-error', 'ai.error_message': 'private-error' }, true);
        expect(JSON.stringify({ attributes: exported.attributes, events: exported.events, status: exported.status })).not.toContain('private-error');
        expect(exported.status.code).toBe(SpanStatusCode.ERROR);
        expect(exported.events[0].attributes?.['exception.type']).toBe('Error');
      });

    test('an explicit false overrides derived error hiding', async () => {
      process.env.FI_HIDE_INPUTS = 'true'; process.env.FI_HIDE_ERROR_DETAILS = ' false ';
      const { exported } = await capture({}, true);
      expect(exported.status.message).toBe('private-error');
    });

    test('drops request headers regardless of content recording', async () => {
      const { exported } = await capture({
        'ai.request.headers.Authorization': 'Bearer synthetic-secret',
        'ai.request.headers.X-Api-Key': 'synthetic-secret',
        'ai.request.headers.x-Custom-Gateway-Credential': 'synthetic-secret',
        'ai.request.headers.Cookie': 'synthetic-secret',
      });
      expect(JSON.stringify(exported.attributes)).not.toContain('synthetic-secret');
    });

    test('exports a sanitized view without mutating another processor’s span', async () => {
      process.env.FI_HIDE_INPUTS = 'true';
      const { exported, original } = await capture({ 'ai.prompt': 'private-input' });
      expect(original.attributes['ai.prompt']).toBe('private-input');
      expect(original.attributes['fi.span.kind']).toBeUndefined();
      expect(exported.spanContext()).toEqual(original.spanContext());
      expect(exported.duration).toEqual(original.duration);
    });

    test('captures environment at construction rather than span end', async () => {
      process.env.FI_HIDE_INPUTS = 'true';
      const exporter = new InMemorySpanExporter();
      const provider = new BasicTracerProvider();
      provider.addSpanProcessor(new Processor({ exporter }));
      delete process.env.FI_HIDE_INPUTS;
      const span = provider.getTracer('privacy').startSpan('snapshot', { attributes: {
        'gen_ai.operation.name': 'chat', 'ai.prompt': 'private-input',
      } });
      span.end();
      await provider.forceFlush();
      const exported = exporter.getFinishedSpans().slice();
      await provider.shutdown();
      expect(exported).toHaveLength(1);
      expect(JSON.stringify(exported[0].attributes)).not.toContain('private-input');
    });

    test('input hiding does not remove output, nor output hiding input', async () => {
      process.env.FI_HIDE_INPUTS = 'true';
      let result = await capture({ 'ai.prompt': 'input-marker', 'ai.response.text': 'output-marker' });
      expect(result.exported.attributes['ai.response.text']).toBe('output-marker');
      expect(result.exported.attributes['ai.prompt']).toBeUndefined();
      delete process.env.FI_HIDE_INPUTS;
      process.env.FI_HIDE_OUTPUTS = 'true';
      result = await capture({ 'ai.prompt': 'input-marker', 'ai.response.text': 'output-marker' });
      expect(result.exported.attributes['ai.prompt']).toBe('input-marker');
      expect(result.exported.attributes['ai.response.text']).toBeUndefined();
    });

    test('keeps content by default', async () => {
      const { exported } = await capture({ 'ai.prompt': 'public-input', 'ai.prompt.tools': ['public-tool-definition'], 'ai.response.text': 'public-output' }, true);
      expect(exported.attributes['ai.prompt']).toBe('public-input');
      expect(exported.attributes['ai.prompt.tools']).toEqual(['public-tool-definition']);
      expect(exported.attributes['ai.response.text']).toBe('public-output');
      expect(exported.status.message).toBe('private-error');
    });
  });
}
