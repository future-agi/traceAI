import { Attributes, diag, SpanStatusCode } from '@opentelemetry/api';
import { BasicTracerProvider, InMemorySpanExporter } from '@opentelemetry/sdk-trace-base';
import { FIBatchSpanProcessor, FISimpleSpanProcessor } from '../FISpanProcessor';
import { isFISpan } from '../utils';

// A privacy switch someone set must never silently do nothing. true, 1, yes and on turn it
// on, false, 0, no and off turn it off (any case, surrounding spaces ignored), and an empty
// value counts as unset. Any other value hides, with one warning that names the variable
// and the accepted values and never repeats the value.

const flags = ['INPUTS', 'OUTPUTS', 'INPUT_MESSAGES', 'OUTPUT_MESSAGES', 'INPUT_TEXT',
  'OUTPUT_TEXT', 'INPUT_IMAGES', 'EMBEDDING_VECTORS', 'ERROR_DETAILS'];
const originalEnv = Object.fromEntries(flags.map(flag => [`FI_HIDE_${flag}`, process.env[`FI_HIDE_${flag}`]]));
let warn: jest.SpyInstance;
beforeEach(() => {
  for (const flag of flags) delete process.env[`FI_HIDE_${flag}`];
  warn = jest.spyOn(diag, 'warn').mockImplementation(() => undefined);
});
afterEach(() => {
  warn.mockRestore();
  for (const [key, value] of Object.entries(originalEnv)) {
    if (value === undefined) delete process.env[key]; else process.env[key] = value;
  }
});

const content: Attributes = { 'ai.prompt': 'private-input', 'input.value': 'private-input', 'ai.response.text': 'private-output' };

async function capture(Processor: typeof FISimpleSpanProcessor | typeof FIBatchSpanProcessor, attributes: Attributes, error = false) {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider();
  // The processor reads the switches when it is constructed.
  provider.addSpanProcessor(new Processor({ exporter, spanFilter: isFISpan }));
  const span = provider.getTracer('privacy').startSpan('native', { attributes: {
    'gen_ai.operation.name': 'chat', 'gen_ai.request.model': 'safe-model', ...attributes,
  } });
  if (error) {
    span.recordException(new Error('private-error'));
    span.setStatus({ code: SpanStatusCode.ERROR, message: 'private-error' });
  }
  span.end();
  await provider.forceFlush();
  const finished = exporter.getFinishedSpans().slice();
  await provider.shutdown();
  expect(finished).toHaveLength(1);
  expect(finished[0].attributes['gen_ai.request.model']).toBe('safe-model');
  return finished[0];
}

for (const Processor of [FISimpleSpanProcessor, FIBatchSpanProcessor]) {
  describe(`${Processor.name} privacy switch values`, () => {
    test.each(['on', 'yes', '1', 'true', ' On ', 'YES'])('FI_HIDE_INPUTS=%j hides input when the span is converted', async value => {
      process.env.FI_HIDE_INPUTS = value;
      const exported = await capture(Processor, content);
      expect(JSON.stringify(exported.attributes)).not.toContain('private-input');
      expect(exported.attributes['ai.response.text']).toBe('private-output');
    });

    test.each(['off', 'no', '0', 'false', ' OFF '])('FI_HIDE_INPUTS=%j keeps input', async value => {
      process.env.FI_HIDE_INPUTS = value;
      const exported = await capture(Processor, content);
      expect(exported.attributes['ai.prompt']).toBe('private-input');
    });

    test.each(['', '   '])('%j counts as unset', async value => {
      process.env.FI_HIDE_INPUTS = value;
      expect((await capture(Processor, content)).attributes['ai.prompt']).toBe('private-input');
      // An unset FI_HIDE_ERROR_DETAILS follows input and output hiding.
      process.env.FI_HIDE_OUTPUTS = 'true'; process.env.FI_HIDE_ERROR_DETAILS = value;
      const exported = await capture(Processor, content, true);
      expect(JSON.stringify({ attributes: exported.attributes, events: exported.events, status: exported.status })).not.toContain('private-error');
    });

    test('an unrecognised value hides when the span is converted', async () => {
      process.env.FI_HIDE_INPUTS = 'enabled-please';
      const exported = await capture(Processor, content);
      expect(JSON.stringify(exported.attributes)).not.toContain('private-input');
      for (const call of warn.mock.calls) expect(String(call[0])).not.toContain('enabled-please');
    });

    test.each(['on', 'yes', '1', 'enabled-please'])('FI_HIDE_ERROR_DETAILS=%j hides error details', async value => {
      process.env.FI_HIDE_ERROR_DETAILS = value;
      const exported = await capture(Processor, {}, true);
      expect(JSON.stringify({ attributes: exported.attributes, events: exported.events, status: exported.status })).not.toContain('private-error');
      expect(exported.status.code).toBe(SpanStatusCode.ERROR);
      for (const call of warn.mock.calls) expect(String(call[0])).not.toContain('enabled-please');
    });

    test.each(['off', 'no', '0', ' Off '])('FI_HIDE_ERROR_DETAILS=%j keeps error details when input is hidden', async value => {
      process.env.FI_HIDE_INPUTS = 'true'; process.env.FI_HIDE_ERROR_DETAILS = value;
      const exported = await capture(Processor, content, true);
      expect(exported.status.message).toBe('private-error');
      expect(JSON.stringify(exported.attributes)).not.toContain('private-input');
    });
  });
}

test('an unrecognised value gives one warning that names the variable and not the value', async () => {
  process.env.FI_HIDE_OUTPUT_TEXT = 'maybe-later';
  for (const Processor of [FISimpleSpanProcessor, FIBatchSpanProcessor]) {
    const exported = await capture(Processor, content);
    expect(JSON.stringify(exported.attributes)).not.toContain('private-output');
  }
  const messages = warn.mock.calls.map(call => String(call[0]));
  const named = messages.filter(message => message.includes('FI_HIDE_OUTPUT_TEXT'));
  expect(named).toHaveLength(1);
  expect(named[0]).toContain('true, 1, yes, on');
  for (const message of messages) expect(message).not.toContain('maybe-later');
});
