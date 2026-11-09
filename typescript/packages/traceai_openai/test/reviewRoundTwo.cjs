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
function instrument(traceConfig = {}) {
  inst = new OpenAIInstrumentation({ instrumentationConfig: { enabled: false }, traceConfig });
  inst.setTracerProvider(provider);
  inst.manuallyInstrument(OpenAI);
}
afterEach(async () => {
  inst?.disable();
  inst = undefined;
  await provider.forceFlush();
  exporter.reset();
});
after(() => provider.shutdown());
const chat = {
  id: 'chat-synthetic', object: 'chat.completion', created: 1, model: 'server-model',
  choices: [{ index: 0, message: { role: 'assistant', content: 'hello' }, finish_reason: 'stop' }],
};
const completion = {
  id: 'completion-synthetic', object: 'text_completion', created: 1, model: 'server-model',
  choices: [{ index: 0, text: 'hello', logprobs: null, finish_reason: 'stop' }],
};
const response = {
  id: 'response-synthetic', object: 'response', created_at: 1, status: 'completed',
  model: 'server-model', output: [], usage: { input_tokens: 2, output_tokens: 1, total_tokens: 3 },
};
function fixture(payload, stream = false) {
  let wire;
  const client = new OpenAI({ apiKey: 'synthetic-not-a-key', maxRetries: 0,
    fetch: async (_url, options) => {
      wire = JSON.parse(options.body);
      return new Response(stream
        ? payload.map(c => 'data: ' + JSON.stringify(c) + '\n\n').join('') + 'data: [DONE]\n\n'
        : JSON.stringify(payload), {
        headers: { 'content-type': stream ? 'text/event-stream' : 'application/json' },
      });
    },
  });
  return { client, getWire: () => wire };
}
function span() {
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 1);
  return spans[0];
}
function noMarker(marker) {
  const attrs = span().attributes;
  assert.deepEqual(Object.keys(attrs).filter(k => JSON.stringify(attrs[k]).includes(marker)), [],
    'synthetic private content must be absent from every attribute');
}
function checkInput(marker, flag) {
  const attrs = span().attributes;
  assert.ok(attrs['gen_ai.request.model'], 'separately recorded model metadata remains available');
  if (flag) {
    noMarker(marker);
    assert.equal(attrs['gen_ai.request.parameters'], '__REDACTED__', 'opaque invocation parameters are input content');
  } else {
    assert.ok(attrs['raw.input'].includes(marker), 'capture-enabled control');
    const parameters = JSON.parse(attrs['gen_ai.request.parameters']);
    assert.ok(!JSON.stringify(parameters).includes(marker), 'input content is not invocation metadata');
    assert.equal(parameters.model, attrs['gen_ai.request.model']);
  }
}
for (const route of ['chat', 'completions', ...(OpenAI.Responses ? ['responses'] : [])]) {
  test(`${route}: nonstream response preserves requested alias`, async () => {
    const payload = route === 'chat' ? chat : route === 'completions' ? completion : response;
    const { client } = fixture(payload);
    const invoke = () => route === 'chat'
      ? client.chat.completions.create({ model: 'requested-alias', messages: [] })
      : route === 'completions'
        ? client.completions.create({ model: 'requested-alias', prompt: 'hello' })
        : client.responses.create({ model: 'requested-alias', input: 'hello' });
    const expected = await invoke();
    instrument();
    assert.deepEqual(await invoke(), expected);
    assert.equal(span().attributes['gen_ai.response.model'], 'server-model');
    assert.equal(span().attributes['gen_ai.request.model'], 'requested-alias');
  });
}
for (const flag of ['hideInputs', 'hideInputText', null]) {
  test(`completions: suffix content respects ${flag ?? 'capture enabled'}`, async () => {
    const marker = 'SYNTHETIC-SUFFIX-PRIVATE';
    const { client, getWire } = fixture(completion);
    const body = { model: 'gpt-3.5-turbo-instruct', prompt: 'hello', suffix: marker };
    const before = structuredClone(body);
    const expected = await client.completions.create(body);
    instrument(flag ? { [flag]: true } : {});
    assert.deepEqual(await client.completions.create(body), expected);
    assert.deepEqual(body, before);
    assert.deepEqual(getWire(), before);
    checkInput(marker, flag);
  });
  // SDK 4.0.0 predates both Responses and predicted output requests.
  if (OpenAI.Responses) for (const arrayContent of [false, true]) {
    test(`chat: ${arrayContent ? 'array' : 'string'} prediction respects ${flag ?? 'capture enabled'}`, async () => {
      const marker = 'SYNTHETIC-PREDICTED-PRIVATE';
      const { client, getWire } = fixture(chat);
      const body = { model: 'gpt-4.1', messages: [{ role: 'user', content: 'Rewrite this document' }],
        prediction: { type: 'content', content: arrayContent ? [{ type: 'text', text: marker }] : marker } };
      const before = structuredClone(body);
      const expected = await client.chat.completions.create(body);
      instrument(flag ? { [flag]: true } : {});
      assert.deepEqual(await client.chat.completions.create(body), expected);
      assert.deepEqual(body, before);
      assert.deepEqual(getWire(), before);
      checkInput(marker, flag);
    });
  }
}
if (OpenAI.Responses) {
  test('Responses: streaming preserves requested model alias', async () => {
    const { client } = fixture([{ type: 'response.completed', sequence_number: 0, response }], true);
    const body = { model: 'requested-alias', input: 'hello', stream: true };
    const expected = [];
    for await (const x of await client.responses.create(body)) expected.push(x);
    instrument();
    const actual = [];
    for await (const x of await client.responses.create(body)) actual.push(x);
    assert.deepEqual(actual, expected);
    assert.equal(span().attributes['gen_ai.response.model'], 'server-model');
    assert.equal(span().attributes['gen_ai.request.model'], 'requested-alias');
  });
  for (const flag of ['hideInputImages', 'hideInputs', null]) {
    for (const field of ['image_url', 'file_id']) {
      test(`Responses: computer screenshot ${field} respects ${flag ?? 'capture enabled'}`, async () => {
        const marker = 'SYNTHETIC-SCREENSHOT-PRIVATE';
        const { client, getWire } = fixture(response);
        const output = { type: 'computer_screenshot',
          [field]: field === 'image_url' ? 'https://example.invalid/' + marker : 'file-' + marker };
        const body = { model: 'computer-use-preview', input: [
          { role: 'user', content: 'keep this visible text' },
          { type: 'computer_call_output', call_id: 'call_synthetic', output },
        ] };
        const before = structuredClone(body);
        const expected = await client.responses.create(body);
        instrument(flag ? { [flag]: true } : {});
        assert.deepEqual(await client.responses.create(body), expected);
        assert.deepEqual(body, before);
        assert.deepEqual(getWire(), before);
        checkInput(marker, flag);
        if (flag !== 'hideInputs') {
          const messages = JSON.parse(span().attributes['gen_ai.input.messages']);
          assert.equal(messages[0].content, 'keep this visible text');
          assert.equal(messages[1].tool_call_id, 'call_synthetic');
          assert.deepEqual(messages[1].content, flag ? [] : [{ ...output, type: 'input_image' }]);
        }
      });
    }
  }
}
