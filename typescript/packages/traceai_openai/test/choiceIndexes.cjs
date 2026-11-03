const { test, after, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const path = require('node:path');
const req = createRequire(path.join(process.env.OPENAI_CONSUMER_DIR, 'package.json'));
const OpenAI = req('openai');
const { OpenAIInstrumentation } = req('@traceai/openai');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = req('@opentelemetry/sdk-trace-base');
const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
let inst;
afterEach(async () => { inst?.disable(); await provider.forceFlush(); exporter.reset(); });
after(() => provider.shutdown());
for (const route of ['chat', 'completions']) {
  for (const together of [false, true]) {
    test(`${route}: n=2 ${together ? 'reordered' : 'interleaved'} choices retain stable identity`, async () => {
      const choices = [[1, 'B0'], [0, 'A0'], [1, 'B1'], [0, 'A1']].map(([index, text], i) => ({
        index,
        ...(route === 'chat' ? { delta: { content: text } } : { text, logprobs: null }),
        finish_reason: i > 1 ? 'stop' : null,
      }));
      const groups = together ? [choices.slice(0, 2), choices.slice(2)] : choices.map(c => [c]);
      const chunks = groups.map(choices => ({ id: 'synthetic', created: 1, model: 'server-model',
        object: route === 'chat' ? 'chat.completion.chunk' : 'text_completion', choices }));
      // Usage-only chunks must still be processed even though they contain no choice zero.
      chunks.push({ ...chunks[0], choices: [], usage: { prompt_tokens: 3, completion_tokens: 4, total_tokens: 7 } });
      const client = new OpenAI({ apiKey: 'synthetic-not-a-key', maxRetries: 0,
        fetch: async () => new Response(
          chunks.map(c => 'data: ' + JSON.stringify(c) + '\n\n').join('') + 'data: [DONE]\n\n',
          { headers: { 'content-type': 'text/event-stream' } }),
      });
      const invoke = () => route === 'chat'
        ? client.chat.completions.create({ model: 'requested-alias', messages: [{ role: 'user', content: 'hello' }], n: 2, stream: true })
        : client.completions.create({ model: 'requested-alias', prompt: 'hello', n: 2, stream: true });
      const baseline = [];
      for await (const c of await invoke()) baseline.push(c);
      inst = new OpenAIInstrumentation({ instrumentationConfig: { enabled: false } });
      inst.setTracerProvider(provider);
      inst.manuallyInstrument(OpenAI);
      const actual = [];
      for await (const c of await invoke()) actual.push(c);
      assert.deepEqual(actual, baseline);
      assert.equal(exporter.getFinishedSpans().length, 1);
      const attrs = exporter.getFinishedSpans()[0].attributes;
      assert.equal(attrs['output.value'], 'A0A1');
      assert.equal(attrs['gen_ai.request.model'], 'requested-alias');
      assert.equal(attrs['gen_ai.response.model'], 'server-model');
      assert.equal(attrs['gen_ai.usage.input_tokens'], 3);
      assert.equal(attrs['gen_ai.usage.output_tokens'], 4);
      assert.deepEqual(JSON.parse(attrs['raw.output']), chunks);
      if (route === 'chat') {
        assert.deepEqual(JSON.parse(attrs['gen_ai.output.messages']), [{ role: 'assistant', content: 'A0A1' }]);
      }
    });
  }
}
