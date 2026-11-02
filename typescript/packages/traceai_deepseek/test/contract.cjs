// Real OpenAI classes, synthetic fetch, actual OTLP/HTTP JSON on loopback only.
const { test, before, after, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { once } = require('node:events');
const { context, trace } = require('@opentelemetry/api');
const { suppressTracing } = require('@opentelemetry/core');
const { NodeTracerProvider } = require('@opentelemetry/sdk-trace-node');
const { SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const { SemanticConventions: S } = require('@traceai/fi-semantic-conventions');
const { DeepSeekInstrumentation, isPatched } = require('../dist/cjs');
const sdk = require(process.env.OPENAI_TEST_MODULE || 'openai');
const OpenAI = sdk.OpenAI || sdk;
const INPUT = 'private-request-marker';
const OUTPUT = 'private-response-marker';
const body = { model: 'deepseek-reasoner', messages: [{ role: 'user', content: INPUT }],
  tools: [{ type: 'function', function: { name: INPUT, description: INPUT, parameters: { type: 'object' } } }] };
const usage = { prompt_tokens: 12, completion_tokens: 8, total_tokens: 20,
  prompt_cache_hit_tokens: 10, prompt_cache_miss_tokens: 2 };
const answer = { id: 'chatcmpl-synthetic', object: 'chat.completion', model: 'deepseek-reasoner-resolved',
  choices: [{ index: 0, message: { role: 'assistant', content: OUTPUT, reasoning_content: OUTPUT,
    tool_calls: [{ id: 'call-1', type: 'function', function: { name: OUTPUT, arguments: JSON.stringify({ value: OUTPUT }) } }] }, finish_reason: 'tool_calls' }], usage };
const chunks = [
  { id: answer.id, model: answer.model, choices: [{ index: 0, delta: { role: 'assistant', reasoning_content: OUTPUT }, finish_reason: null }] },
  { id: answer.id, model: answer.model, choices: [{ index: 0, delta: { content: OUTPUT, tool_calls: [
    { index: 0, id: 'call-1', type: 'function', function: { name: OUTPUT, arguments: '{"value":' } }] }, finish_reason: null }] },
  { id: answer.id, model: answer.model, choices: [{ index: 0, delta: { tool_calls: [
    { index: 0, function: { arguments: JSON.stringify(OUTPUT) + '}' } }] }, finish_reason: 'tool_calls' }] },
  { id: answer.id, model: answer.model, choices: [], usage },
  { id: answer.id, model: answer.model, choices: [] }, // usage must survive a later empty chunk
];
let server, port, wire, provider, exporter, inst, activeSpan;
before(async () => {
  server = http.createServer(async (req, res) => {
    const data = [];
    for await (const part of req) data.push(part);
    assert.equal(req.url, '/v1/traces');
    wire.push(JSON.parse(Buffer.concat(data)));
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end('{}');
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  port = server.address().port;
});
after(() => new Promise(resolve => server.close(resolve)));
beforeEach(() => {
  wire = [];
  activeSpan = undefined;
  exporter = new InMemorySpanExporter();
  provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter),
    new SimpleSpanProcessor(new OTLPTraceExporter({ url: `http://127.0.0.1:${port}/v1/traces` }))] });
  provider.register();
  configure();
});
afterEach(async () => {
  inst.disable();
  await provider.shutdown();
  trace.disable();
  context.disable();
});
function configure(traceConfig) {
  inst?.disable();
  // Provider-first is the documented setup and previously lost privacy options.
  inst = new DeepSeekInstrumentation({ traceConfig });
  inst.setTracerProvider(provider);
  inst.manuallyInstrument(sdk);
}
function client(baseURL = 'https://api.deepseek.com/v1', mode = 'chat') {
  return new OpenAI({ apiKey: 'synthetic-not-a-real-key', baseURL, maxRetries: 0,
    fetch: async (_url, options) => {
      activeSpan = trace.getActiveSpan()?.spanContext();
      const request = JSON.parse(options.body);
      if (mode === 'error') return new Response(JSON.stringify({ error: { message: `${INPUT} ${OUTPUT}`, type: 'test_error' } }),
        { status: 400, headers: { 'content-type': 'application/json' } });
      const headers = { 'content-type': request.stream ? 'text/event-stream' : 'application/json', 'x-request-id': 'request-synthetic' };
      if (!request.stream) return new Response(JSON.stringify(answer), { headers });
      const data = chunks.map(chunk => `data: ${JSON.stringify(chunk)}\n\n`).join('');
      return new Response(data + (mode === 'stream-error' ? 'data: not-json\n\n' : 'data: [DONE]\n\n'), { headers });
    } });
}
async function collect(iterable) {
  const values = [];
  for await (const value of iterable) values.push(value);
  return values;
}
async function spans() { await provider.forceFlush(); return exporter.getFinishedSpans(); }
function serializedWire() { return JSON.stringify(wire); }
function wireSpans() {
  return wire.flatMap(request => request.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans)));
}
async function assertOne() {
  const found = await spans();
  assert.equal(found.length, 1);
  assert.equal(wireSpans().length, 1);
  assert.equal(found[0].attributes[S.LLM_PROVIDER], 'deepseek');
  return found[0];
}

test('real resource is patched once and exports model, reasoning, tools and cache counts', async () => {
  assert.equal(OpenAI.prototype.chat, undefined);
  assert.equal(isPatched(), true);
  inst.manuallyInstrument(sdk);
  const result = await client().chat.completions.create(body);
  assert.equal(result.choices[0].message.content, OUTPUT);
  const span = await assertOne();
  assert.equal(activeSpan.spanId, span.spanContext().spanId);
  assert.equal(span.attributes[S.LLM_MODEL_NAME], body.model);
  assert.equal(span.attributes[S.GEN_AI_RESPONSE_MODEL], answer.model);
  assert.equal(span.attributes[S.GEN_AI_RESPONSE_ID], answer.id);
  assert.equal(span.attributes['deepseek.reasoning_content'], OUTPUT);
  assert.equal(span.attributes['deepseek.prompt_cache_hit_tokens'], 10);
  assert.equal(span.attributes['deepseek.prompt_cache_miss_tokens'], 2);
  assert.equal(span.attributes[S.LLM_TOKEN_COUNT_TOTAL], 20);
  assert.equal(JSON.parse(span.attributes[S.LLM_OUTPUT_MESSAGES])[0].tool_calls[0].id, 'call-1');
  assert.ok(serializedWire().includes(OUTPUT));
});

test('normalizes exact hosts but does not trace other providers or lookalike URLs', async () => {
  for (const host of ['https://api.openai.com/v1', 'https://deepseek.example.com/v1',
    'https://api.deepseek.com.evil.example/v1', 'https://evil.example/api.deepseek.com',
    'https://evil.example/?api.deepseek.com', 'https://api.deepseek.com@evil.example/v1']) {
    await client(host).chat.completions.create(body);
  }
  assert.equal((await spans()).length, 0);
  await client('https://API.DEEPSEEK.COM/v1').chat.completions.create(body);
  await assertOne();
});

test('keeps APIPromise helpers and raw response body ownership', async () => {
  const promise = client().chat.completions.create(body);
  assert.equal(typeof promise.withResponse, 'function');
  const response = await promise.asResponse();
  assert.equal(response.bodyUsed, false);
  assert.equal((await response.json()).id, answer.id);
  await assertOne();
});

test('withResponse and repeated awaits return the same parsed value and one span', async () => {
  const promise = client().chat.completions.create(body);
  const { data, response } = await promise.withResponse();
  assert.equal(response.headers.get('x-request-id'), 'request-synthetic');
  assert.equal(await promise, data);
  await assertOne();
});

test('preserves a complete stream and aggregates reasoning, split tool calls and cache usage', async () => {
  const promise = client().chat.completions.create({ ...body, stream: true });
  assert.equal(typeof promise.withResponse, 'function');
  const { data: stream } = await promise.withResponse();
  assert.equal((await spans()).length, 0);
  assert.ok(stream.controller instanceof AbortController);
  assert.deepEqual(await collect(stream), chunks);
  const span = await assertOne();
  assert.equal(span.status.code, 1);
  assert.equal(span.attributes[S.OUTPUT_VALUE], OUTPUT);
  assert.equal(span.attributes['deepseek.reasoning_content'], OUTPUT);
  assert.equal(span.attributes['deepseek.prompt_cache_hit_tokens'], 10);
  assert.equal(span.attributes['deepseek.prompt_cache_miss_tokens'], 2);
  const message = JSON.parse(span.attributes[S.LLM_OUTPUT_MESSAGES])[0];
  assert.deepEqual(message.tool_calls, answer.choices[0].message.tool_calls);
});

test('retains native tee and readable-stream helpers where available', async () => {
  const stream = await client().chat.completions.create({ ...body, stream: true });
  if (typeof stream.tee === 'function') {
    const [left, right] = stream.tee();
    assert.deepEqual(await collect(left), chunks);
    assert.deepEqual(await collect(right), chunks);
  } else {
    assert.deepEqual(await collect(stream), chunks);
  }
  await assertOne();
});

test('finishes partial streams when callers break and preserves cancellation', async () => {
  const stream = await client().chat.completions.create({ ...body, stream: true });
  for await (const chunk of stream) { assert.deepEqual(chunk, chunks[0]); break; }
  assert.equal(stream.controller.signal.aborted, true);
  await assertOne();
});

test('ends a stream cancelled before any consumption', async () => {
  const stream = await client().chat.completions.create({ ...body, stream: true });
  stream.controller.abort();
  await assertOne();
});

test('exports iteration errors as ERROR even when the SDK aborts internally', async () => {
  const stream = await client(undefined, 'stream-error').chat.completions.create({ ...body, stream: true });
  await assert.rejects(async () => { for await (const _ of stream) {} });
  assert.equal((await assertOne()).status.code, 2);
});

test('passes HTTP errors to the caller and exports one ERROR span', async () => {
  await assert.rejects(client(undefined, 'error').chat.completions.create(body), /private-request-marker/);
  const span = await assertOne();
  assert.equal(span.status.code, 2);
});

test('suppression bypasses tracing without dropping the request', async () => {
  await context.with(suppressTracing(context.active()), () => client().chat.completions.create(body));
  assert.equal((await spans()).length, 0);
});

test('disable restores the resource; enable reinstalls manual instrumentation', async () => {
  inst.disable();
  assert.equal(isPatched(), false);
  await client().chat.completions.create(body);
  assert.equal((await spans()).length, 0);
  inst.enable();
  await client().chat.completions.create(body);
  await assertOne();
});

test('a second instrumentor does not duplicate or remove the owning wrapper', async () => {
  const other = new DeepSeekInstrumentation({ instrumentationConfig: { enabled: false } });
  other.setTracerProvider(provider);
  other.manuallyInstrument(sdk);
  other.disable();
  assert.equal(isPatched(), true);
  await client().chat.completions.create(body);
  await assertOne();
});

test('unsupported module shapes never report a successful patch', () => {
  inst.disable();
  inst.manuallyInstrument({ OpenAI: class Unsupported {} });
  assert.equal(isPatched(), false);
});

for (const flag of ['hideInputs', 'hideInputMessages', 'hideInputText', 'hideInputImages']) {
  test(`${flag} removes request content, raw bodies, tool schemas and invocation copies on the OTLP wire`, async () => {
    configure({ [flag]: true });
    await client().chat.completions.create(body);
    await assertOne();
    assert.ok(!serializedWire().includes(INPUT));
    assert.ok(serializedWire().includes(OUTPUT));
  });
}
for (const flag of ['hideOutputs', 'hideOutputMessages', 'hideOutputText']) {
  for (const stream of [false, true]) {
    test(`${flag} hides reasoning, tools and all response copies (stream=${stream})`, async () => {
      configure({ [flag]: true });
      const result = await client().chat.completions.create({ ...body, stream });
      if (stream) await collect(result);
      const span = await assertOne();
      assert.ok(!serializedWire().includes(OUTPUT));
      assert.ok(serializedWire().includes(INPUT));
      assert.equal(span.attributes['deepseek.prompt_cache_hit_tokens'], 10);
    });
  }
}

test('environment privacy flags are honored for reasoning and raw output', async () => {
  const previous = process.env.FI_HIDE_OUTPUTS;
  process.env.FI_HIDE_OUTPUTS = 'true';
  try {
    await client().chat.completions.create(body);
    await assertOne();
    assert.ok(!serializedWire().includes(OUTPUT));
  } finally {
    if (previous === undefined) delete process.env.FI_HIDE_OUTPUTS;
    else process.env.FI_HIDE_OUTPUTS = previous;
  }
});

test('raw-response errors end one ERROR span without consuming a response', async () => {
  const promise = client(undefined, 'error').chat.completions.create(body);
  await assert.rejects(promise.asResponse(), /private-response-marker/);
  assert.equal((await assertOne()).status.code, 2);
});

test('iterator return before the first next ends the span', async () => {
  const stream = await client().chat.completions.create({ ...body, stream: true });
  const iterator = stream[Symbol.asyncIterator]();
  await iterator.return();
  await assertOne();
});

test('derived APIPromises used by SDK parsers remain traced', async () => {
  const promise = client().chat.completions.create(body)._thenUnwrap(value => ({ ...value, parsed: true }));
  const { data } = await promise.withResponse();
  assert.equal(data.parsed, true);
  await assertOne();
});

test('public parse helper retains tracing when the SDK provides it', async () => {
  const completions = client().chat.completions;
  if (!completions.parse) return;
  const result = await completions.parse({ model: body.model, messages: body.messages });
  assert.equal(result.choices[0].message.content, OUTPUT);
  await assertOne();
});

test('native readable stream conversion still observes consumption', async () => {
  const stream = await client().chat.completions.create({ ...body, stream: true });
  if (stream.toReadableStream) {
    const bytes = await collect(stream.toReadableStream());
    assert.ok(bytes.length > 0);
  } else { await collect(stream); }
  await assertOne();
});

test('synchronous SDK failures retain their identity and export ERROR', async () => {
  const error = new Error('synthetic synchronous failure');
  const wrapper = inst.createChatCompletionWrapper(inst)(() => { throw error; });
  assert.throws(() => wrapper.call({ _client: { baseURL: 'https://api.deepseek.com' } }, body), e => e === error);
  assert.equal((await assertOne()).status.code, 2);
});

test('privacy hides echoed provider errors while preserving ERROR and caller exceptions', async () => {
  configure({ hideInputs: true, hideOutputs: true });
  await assert.rejects(client(undefined, 'error').chat.completions.create(body), /private-response-marker/);
  assert.equal((await assertOne()).status.code, 2);
  assert.ok(!serializedWire().includes(INPUT));
  assert.ok(!serializedWire().includes(OUTPUT));
});

test('does not claim DeepSeek Responses coverage or change that SDK resource', async () => {
  const sdkClient = client();
  if (!sdkClient.responses) return; // OpenAI 4.0 predates Responses.
  const promise = sdkClient.responses.create({ model: 'synthetic', input: INPUT });
  assert.equal(typeof promise.withResponse, 'function');
  await promise;
  assert.equal((await spans()).length, 0);
});
