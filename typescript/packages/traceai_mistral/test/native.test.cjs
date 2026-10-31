const { test, after } = require('node:test');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const assert = require('node:assert/strict');
const http = require('node:http');
const { once } = require('node:events');
const { context, trace, SpanStatusCode } = require('@opentelemetry/api');
const { suppressTracing } = require('@opentelemetry/core');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const { SemanticConventions: S } = require('@traceai/fi-semantic-conventions');
const { MistralInstrumentation, isPatched } = require('../dist/src');

const manager = new AsyncLocalStorageContextManager().enable();
context.setGlobalContextManager(manager);
after(() => { context.disable(); manager.disable(); });
const request = { model: 'mistral-test', messages: [{ role: 'user', content: 'synthetic input' }] };
const completion = () => ({
  id: 'completion-test', object: 'chat.completion', created: 1, model: 'mistral-test',
  choices: [{ index: 0, message: { role: 'assistant', content: 'synthetic output', tool_calls: [{ id: 'tool-1', type: 'function', function: { name: 'lookup', arguments: '{"city":"Paris"}' } }] }, finish_reason: 'tool_calls' }],
  usage: { prompt_tokens: 3, completion_tokens: 5, total_tokens: 8 },
});
const chunk = (content = 'hello', extra = {}) => ({
  id: 'stream-test', object: 'chat.completion.chunk', created: 1, model: 'mistral-test',
  choices: [{ index: 0, delta: { role: 'assistant', content }, finish_reason: null }], ...extra,
});
const jsonResponse = value => new Response(JSON.stringify(value), { headers: { 'content-type': 'application/json' } });
const sseResponse = events => new Response(events.map(e => `data: ${JSON.stringify(e)}\n\n`).join('') + 'data: [DONE]\n\n', { headers: { 'content-type': 'text/event-stream' } });

function fixture(t, config, Exporter = InMemorySpanExporter) {
  const exporter = new Exporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const instrumentation = new MistralInstrumentation({ traceConfig: config });
  instrumentation.setTracerProvider(provider);
  t.after(async () => { instrumentation.disable(); await provider.shutdown(); });
  return { exporter, provider, instrumentation, spans: () => exporter.getFinishedSpans() };
}
async function modern(t, sdk = 'mistral-current', config) {
  const mod = await import(sdk);
  const f = fixture(t, config);
  const calls = [];
  let respond = () => jsonResponse(completion());
  const httpClient = { request: async req => { calls.push(req); return respond(req); } };
  const client = new mod.Mistral({ apiKey: 'synthetic-not-a-key', serverURL: 'http://localhost:9999', httpClient });
  return { ...f, mod, client, calls, httpClient, respond: fn => { respond = fn; } };
}

for (const sdk of ['@mistralai/mistralai', 'mistral-current']) {
  test(`${sdk}: pre-existing resource, new client, options, context and camelCase response`, async t => {
    const f = await modern(t, sdk);
    const resource = f.client.chat;
    const original = resource.complete;
    const options = f.client._options;
    f.instrumentation.manuallyInstrument(f.mod);
    assert.notEqual(resource.complete, original);
    f.instrumentation.manuallyInstrument(f.mod);
    const controller = new AbortController();
    const parent = f.provider.getTracer('test').startSpan('parent');
    const response = await context.with(trace.setSpan(context.active(), parent), () => resource.complete(request, {
      serverURL: 'http://localhost:6789', fetchOptions: { headers: { 'x-synthetic-option': 'preserved' }, signal: controller.signal },
    }));
    assert.equal(response.choices[0].message.content, 'synthetic output');
    assert.equal(f.client._options, options);
    assert.match(f.calls[0].url, /^http:\/\/localhost:6789/);
    assert.equal(f.calls[0].headers.get('x-synthetic-option'), 'preserved');
    controller.abort();
    assert.equal(f.calls[0].signal.aborted, true);
    const span = f.spans()[0];
    assert.equal(span.parentSpanContext.spanId, parent.spanContext().spanId);
    assert.equal(span.attributes[S.LLM_TOKEN_COUNT_PROMPT], 3);
    assert.equal(span.attributes[S.LLM_TOKEN_COUNT_COMPLETION], 5);
    assert.deepEqual(JSON.parse(span.attributes[S.GEN_AI_RESPONSE_FINISH_REASONS]), ['tool_calls']);
    assert.match(span.attributes[S.LLM_OUTPUT_MESSAGES], /lookup/);
    const next = new f.mod.Mistral({ apiKey: 'synthetic-not-a-key', httpClient: f.httpClient });
    await next.chat.complete(request);
    assert.equal(f.spans().length, 2);
    assert.ok(isPatched());
    f.instrumentation.disable();
    assert.equal(resource.complete, original);
    await next.chat.complete(request);
    assert.equal(f.spans().length, 2);
    f.instrumentation.enable();
    await resource.complete(request);
    assert.equal(f.spans().length, 3);
    parent.end();
  });
}

test('CommonJS import hook instruments native SDK and supports disable/re-enable', () => {
  execFileSync(process.execPath, [path.join(__dirname, 'fixtures/cjs-hook.cjs')], { timeout: 15000 });
});

test('explicit registration does not retain discarded per-request clients', () => {
  execFileSync(process.execPath, ['--expose-gc', path.join(__dirname, 'fixtures/client-gc.cjs')], { timeout: 15000 });
});

test('explicit instance registration preserves identity and composes without duplicate spans', async t => {
  const f = await modern(t);
  const original = f.client.chat.complete;
  assert.equal(f.instrumentation.instrumentClient(f.client), f.client);
  f.instrumentation.instrumentClient(f.client);
  const other = new MistralInstrumentation();
  t.after(() => other.disable());
  other.setTracerProvider(f.provider);
  other.instrumentClient(f.client);
  other.disable();
  await f.client.chat.complete(request);
  assert.equal(f.spans().length, 1);
  f.instrumentation.disable();
  assert.equal(f.client.chat.complete, original);
  assert.equal(Object.hasOwn(f.client.chat, 'complete'), false);
  f.instrumentation.enable();
  await f.client.chat.complete(request);
  assert.equal(f.spans().length, 2);
});

test('provider configured after manual patch is used; suppressed calls create no span', async t => {
  const f = await modern(t);
  f.instrumentation.manuallyInstrument(f.mod);
  f.instrumentation.setTracerProvider(f.provider);
  await context.with(suppressTracing(context.active()), () => f.client.chat.complete(request));
  assert.equal(f.spans().length, 0);
  await f.client.chat.complete(request);
  assert.equal(f.spans().length, 1);
});

test('read-only ESM namespace and real ESM build support manual registration', async t => {
  const f = await modern(t);
  f.instrumentation.disable();
  const esm = await import('../dist/esm/index.js');
  const instrumentation = new esm.MistralInstrumentation();
  t.after(() => instrumentation.disable());
  instrumentation.setTracerProvider(f.provider);
  instrumentation.manuallyInstrument(f.mod);
  await f.client.chat.complete(request);
  assert.equal(f.spans().length, 1);
  assert.equal(Object.hasOwn(f.mod, 'fiPatched'), false);
});

test('mixed CJS and ESM registration does not create duplicate spans', async t => {
  const f = await modern(t);
  f.instrumentation.manuallyInstrument(f.mod);
  const esm = await import('../dist/esm/index.js');
  const other = new esm.MistralInstrumentation();
  other.setTracerProvider(f.provider);
  t.after(() => other.disable());
  other.instrumentClient(f.client);
  await f.client.chat.complete(request);
  assert.equal(f.spans().length, 1);
  other.disable();
  await f.client.chat.complete(request);
  assert.equal(f.spans().length, 2);
});

for (const sdk of ['mistral-floor', 'mistral-v1']) {
  test(`${sdk}: real legacy client chat, embeddings, streaming and lifecycle`, async t => {
    const { default: Client } = await import(sdk);
    const f = fixture(t);
    const client = new Client('synthetic-not-a-key', 'http://localhost:9999');
    let received;
    client._request = async (_method, path, body) => {
      received = body;
      if (body.stream) return (async function* () { yield Buffer.from(`data: ${JSON.stringify(chunk())}\n`); })();
      if (path.endsWith('embeddings')) return { model: 'embed-test', data: [{ index: 0, embedding: [1, 2] }], usage: { prompt_tokens: 1, total_tokens: 1 } };
      return completion();
    };
    const original = client.chat;
    f.instrumentation.instrumentClient(client);
    const positional = sdk === 'mistral-v1';
    const chatArgs = positional ? [request.model, request.messages] : [request];
    await client.chat(...chatArgs);
    assert.equal(received.model, request.model);
    assert.equal(f.spans()[0].attributes[S.LLM_MODEL_NAME], request.model);
    assert.equal(f.spans()[0].attributes[S.LLM_TOKEN_COUNT_PROMPT], 3);
    await client.embeddings(...(positional ? ['embed-test', ['text']] : [{ model: 'embed-test', input: ['text'] }]));
    assert.equal(f.spans()[1].attributes[S.LLM_TOKEN_COUNT_TOTAL], 1);
    const stream = client.chatStream(...chatArgs);
    assert.equal(f.spans().length, 2);
    const values = [];
    for await (const value of stream) values.push(value);
    assert.equal(values.length, 1);
    assert.equal(f.spans().length, 3);
    f.instrumentation.disable();
    assert.equal(client.chat, original);
    await client.chat(...chatArgs);
    assert.equal(f.spans().length, 3);
    f.instrumentation.enable();
    await client.chat(...chatArgs);
    assert.equal(f.spans().length, 4);
  });
}

test('real current SDK agents, embeddings and conversations use native options and response shapes', async t => {
  const f = await modern(t);
  f.instrumentation.manuallyInstrument(f.mod);
  await f.client.agents.complete({ agentId: 'synthetic-agent', messages: request.messages });
  assert.match(f.calls[0].url, /agents\/completions/);
  f.respond(() => jsonResponse({ id: 'embedding-test', object: 'list', model: 'embed-test', data: [{ object: 'embedding', index: 0, embedding: [0.1, 0.2] }], usage: { prompt_tokens: 2, total_tokens: 2 } }));
  await f.client.embeddings.create({ model: 'embed-test', inputs: ['text'] });
  assert.equal(f.spans()[1].attributes[S.LLM_TOKEN_COUNT_PROMPT], 2);
  assert.deepEqual(f.spans()[1].attributes[`${S.EMBEDDING_EMBEDDINGS}.0.${S.EMBEDDING_VECTOR}`], [0.1, 0.2]);
  f.respond(() => jsonResponse({ object: 'conversation.response', conversation_id: 'conversation-test', outputs: [], usage: { prompt_tokens: 2, completion_tokens: 3, total_tokens: 5 } }));
  await f.client.beta.conversations.start({ model: request.model, inputs: 'hello' });
  await f.client.beta.conversations.append({ conversationId: 'conversation-test', conversationAppendRequest: { inputs: 'again' } });
  assert.equal(f.spans().length, 4);
  assert.equal(f.spans()[2].attributes[S.GEN_AI_RESPONSE_ID], 'conversation-test');
  assert.equal(f.spans()[3].attributes[S.LLM_TOKEN_COUNT_TOTAL], 5);
});

test('real SSE stream remains open until consumption and aggregates tool deltas and final usage', async t => {
  const f = await modern(t);
  f.instrumentation.manuallyInstrument(f.mod);
  const tool = (name, args) => [{ index: 0, delta: { tool_calls: [{ index: 0, id: 'tool-1', type: 'function', function: { name, arguments: args } }] }, finish_reason: null }];
  f.respond(() => sseResponse([
    chunk('hello'), chunk('', { choices: tool('lookup', '{"city":') }),
    chunk('', { choices: tool('', '"Paris"}') }),
    chunk('', { choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }], usage: { prompt_tokens: 3, completion_tokens: 5, total_tokens: 8 } }),
  ]));
  const stream = await f.client.chat.stream(request);
  assert.ok(stream instanceof ReadableStream);
  assert.equal(f.spans().length, 0);
  const values = [];
  for await (const value of stream) { assert.equal(f.spans().length, 0); values.push(value); }
  assert.equal(values.length, 4);
  assert.equal(f.spans().length, 1);
  const attrs = f.spans()[0].attributes;
  assert.equal(attrs[S.LLM_TOKEN_COUNT_TOTAL], 8);
  const message = JSON.parse(attrs[S.LLM_OUTPUT_MESSAGES])[0];
  assert.equal(message.content, 'hello');
  assert.equal(message.tool_calls[0].function.name, 'lookup');
  assert.equal(message.tool_calls[0].function.arguments, '{"city":"Paris"}');
});

for (const mode of ['break', 'reader', 'cancel', 'pipeTo', 'tee']) {
  test(`current SSE lifecycle through ${mode}`, async t => {
    const f = await modern(t);
    f.instrumentation.instrumentClient(f.client);
    f.respond(() => sseResponse([chunk('one'), chunk('two')]));
    const stream = await f.client.agents.stream({ agentId: 'synthetic-agent', messages: request.messages });
    assert.equal(f.spans().length, 0);
    if (mode === 'break') { for await (const _ of stream) break; }
    if (mode === 'reader') {
      const reader = stream.getReader();
      assert.equal((await reader.read()).done, false);
      await reader.cancel(); reader.releaseLock();
    }
    if (mode === 'cancel') await stream.cancel();
    if (mode === 'pipeTo') await stream.pipeTo(new WritableStream({ write() {} }));
    if (mode === 'tee') {
      const [left, right] = stream.tee();
      await Promise.all([left.pipeTo(new WritableStream()), right.pipeTo(new WritableStream())]);
    }
    assert.equal(f.spans().length, 1);
    assert.equal(f.spans()[0].status.code, SpanStatusCode.OK);
  });
}

test('sync errors, request rejections and iterator errors preserve exception identity and end once', async t => {
  const f = fixture(t);
  const error = new Error('synthetic provider error');
  const client = { chat: { complete() { throw error; }, async stream() { return (async function* () { yield chunk(); throw error; })(); } } };
  f.instrumentation.instrumentClient(client);
  assert.throws(() => client.chat.complete(request), e => e === error);
  const stream = await client.chat.stream(request);
  await assert.rejects(async () => { for await (const _ of stream) {} }, e => e === error);
  const reject = { chat: { complete: () => Promise.reject(error) } };
  f.instrumentation.instrumentClient(reject);
  await assert.rejects(reject.chat.complete(request), e => e === error);
  assert.equal(f.spans().length, 3);
  for (const span of f.spans()) { assert.equal(span.status.code, SpanStatusCode.ERROR); assert.equal(span.events.length, 1); }
});

test('iterator return before next forwards cancellation and ends the span', async t => {
  const f = fixture(t);
  let cancelled = 0;
  const iterator = { next: async () => ({ done: false, value: chunk() }), return: async () => { cancelled++; return { done: true }; }, [Symbol.asyncIterator]() { return this; } };
  const client = { chat: { stream: () => iterator } };
  f.instrumentation.instrumentClient(client);
  const stream = client.chat.stream(request);
  await stream[Symbol.asyncIterator]().return();
  assert.equal(cancelled, 1);
  assert.equal(f.spans().length, 1);
});

test('stream output memory is bounded while late token usage is retained', async t => {
  const f = fixture(t);
  const client = { chat: { stream: async function* () {
    for (let i = 0; i < 100; i++) yield chunk('x'.repeat(10000));
    yield chunk('', { choices: [], usage: { prompt_tokens: 1, completion_tokens: 1000, total_tokens: 1001 } });
  } } };
  f.instrumentation.instrumentClient(client);
  for await (const _ of client.chat.stream(request)) {}
  const attrs = f.spans()[0].attributes;
  assert.ok(attrs[S.OUTPUT_VALUE].length < 65536);
  assert.equal(attrs['mistral.output.truncated'], true);
  assert.equal(attrs[S.LLM_TOKEN_COUNT_TOTAL], 1001);
});

test('telemetry failure logs a warning without failing or retrying the SDK call', async t => {
  const f = fixture(t);
  const { diag, DiagLogLevel } = require('@opentelemetry/api');
  const warnings = [];
  diag.setLogger({ warn: (...args) => warnings.push(args), error() {}, debug() {}, info() {}, verbose() {} }, DiagLogLevel.WARN);
  t.after(() => diag.disable());
  let calls = 0;
  const result = completion();
  const client = { chat: { complete: async () => { calls++; return result; } } };
  f.instrumentation.instrumentClient(client);
  f.instrumentation.setTracerProvider({ getTracer: () => ({ startSpan() { throw new Error('synthetic telemetry error'); } }) });
  assert.equal(await client.chat.complete(request), result);
  assert.equal(calls, 1);
  assert.ok(warnings.length > 0);
});

for (const fails of [false, true]) {
  test(`real conversation SSE captures tools and usage, provider error=${fails}`, async t => {
    const f = await modern(t, 'mistral-current', { hideErrorDetails: true });
    f.instrumentation.instrumentClient(f.client);
    const events = [
      { type: 'conversation.response.started', conversation_id: 'conversation-test' },
      { type: 'message.output.delta', id: 'message-1', role: 'assistant', content: 'hello', model: 'mistral-test' },
      { type: 'function.call.delta', id: 'call-1', output_index: 1, tool_call_id: 'tool-1', name: 'lookup', arguments: '{}' },
      fails ? { type: 'conversation.response.error', message: 'PRIVATE_ERROR_CANARY', code: 500 } : { type: 'conversation.response.done', usage: { prompt_tokens: 3, completion_tokens: 5, total_tokens: 8 } },
    ];
    f.respond(() => new Response(events.map(e => `event: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`).join(''), { headers: { 'content-type': 'text/event-stream' } }));
    const stream = await f.client.beta.conversations.startStream({ model: request.model, inputs: 'hello' });
    const received = [];
    for await (const event of stream) received.push(event);
    assert.equal(received.length, events.length);
    assert.equal(f.spans().length, 1);
    const span = f.spans()[0];
    assert.equal(span.attributes[S.GEN_AI_RESPONSE_ID], 'conversation-test');
    assert.match(span.attributes[S.LLM_OUTPUT_MESSAGES], /lookup/);
    assert.equal(span.status.code, fails ? SpanStatusCode.ERROR : SpanStatusCode.OK);
    if (fails) assert.equal(JSON.stringify({ attributes: span.attributes, events: span.events, status: span.status }).includes('PRIVATE_ERROR_CANARY'), false);
    else assert.equal(span.attributes[S.LLM_TOKEN_COUNT_TOTAL], 8);
  });
}

test('localhost OTLP wire honors privacy for chat, streaming, embeddings, tools and errors', async t => {
  const payloads = [];
  const server = http.createServer(async (req, res) => {
    const buffers = [];
    for await (const part of req) buffers.push(part);
    payloads.push(JSON.parse(Buffer.concat(buffers).toString()));
    res.writeHead(200, { 'content-type': 'application/json' }); res.end('{}');
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => new Promise(resolve => server.close(resolve)));
  const exporter = new OTLPTraceExporter({ url: `http://127.0.0.1:${server.address().port}/v1/traces` });
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const instrumentation = new MistralInstrumentation({ traceConfig: { hideInputs: true, hideOutputs: true, hideEmbeddingVectors: true } });
  instrumentation.setTracerProvider(provider);
  t.after(async () => { instrumentation.disable(); await provider.shutdown(); });
  const secret = 'SYNTHETIC_PRIVATE_CANARY';
  const client = {
    chat: {
      complete: async () => ({ ...completion(), choices: [{ index: 0, message: { role: 'assistant', content: secret, toolCalls: [{ function: { name: 'lookup', arguments: secret } }] } }] }),
      stream: async function* () { yield chunk(secret); throw new Error(secret); },
    },
    embeddings: { create: async () => ({ data: [{ embedding: [912345.6] }], usage: { promptTokens: 1, totalTokens: 1 } }) },
  };
  instrumentation.instrumentClient(client);
  await client.chat.complete({ ...request, messages: [{ role: 'user', content: secret }], tools: [{ description: secret }] });
  await assert.rejects(async () => { for await (const _ of client.chat.stream({ ...request, messages: [{ role: 'user', content: secret }] })) {} });
  await client.embeddings.create({ model: 'embed-test', inputs: [secret] });
  await provider.forceFlush();
  const wire = JSON.stringify(payloads);
  assert.equal(wire.includes(secret), false);
  assert.equal(wire.includes('912345.6'), false);
  const spans = payloads.flatMap(p => p.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
  assert.equal(spans.length, 3);
  assert.equal(spans.filter(s => s.status.code === SpanStatusCode.ERROR).length, 1);
  assert.ok(wire.includes(S.LLM_PROVIDER));
});
