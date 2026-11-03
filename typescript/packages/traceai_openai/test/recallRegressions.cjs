// Real installed SDK + packed current integration. Synthetic fetch only; run under sandbox-exec.
const { test, after, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { getEventListeners } = require('node:events');
const path = require('node:path');
const req = createRequire(path.resolve(process.env.OPENAI_CONSUMER_DIR, 'package.json'));
const OpenAI = req('openai');
const sdkVersion = req(path.resolve(process.env.OPENAI_CONSUMER_DIR, 'node_modules/openai/package.json')).version;
const { OpenAIInstrumentation } = req('@traceai/openai');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = req('@opentelemetry/sdk-trace-base');
const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
let instrumentation;
function instrument(config = {}) {
  instrumentation = new OpenAIInstrumentation({ instrumentationConfig: { enabled: false }, traceConfig: config });
  instrumentation.setTracerProvider(provider);
  instrumentation.manuallyInstrument(OpenAI);
}
afterEach(async () => { instrumentation?.disable(); instrumentation = undefined; await provider.forceFlush(); exporter.reset(); });
after(async () => { await provider.shutdown(); });
const tick = () => new Promise(resolve => setImmediate(resolve));
const chat = { id: 'chat-offline', object: 'chat.completion', model: 'returned-model', choices: [{ index: 0, message: { role: 'assistant', content: 'SYNTHETIC-ANSWER' }, finish_reason: 'stop' }], usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 } };
const response = { id: 'resp-offline', object: 'response', status: 'completed', model: 'returned-model', output: [], usage: { input_tokens: 2, output_tokens: 1, total_tokens: 3 } };
const toolCall = { id: 'call-synthetic', type: 'custom', custom: { name: 'code_exec', input: 'print(1)' } };
function client(body, metrics = {}) {
  return new OpenAI({ apiKey: 'synthetic-not-a-key', maxRetries: 0, fetch: async (_url, options) => {
    metrics.requests = (metrics.requests || 0) + 1;
    metrics.body = JSON.parse(options.body);
    return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
  } });
}
function span() { const spans = exporter.getFinishedSpans(); assert.equal(spans.length, 1); return spans[0]; }
function streamingClient(operation, { unfinished = false, usage = false } = {}) {
  const chunk = operation === 'chat'
    ? { id: 'chat-synthetic', object: 'chat.completion.chunk', model: 'returned-model', choices: [{ index: 0, delta: { content: 'SYNTHETIC-ANSWER' }, finish_reason: null }] }
    : { id: 'cmpl-synthetic', object: 'text_completion', model: 'returned-model', choices: [{ index: 0, text: 'SYNTHETIC-ANSWER', finish_reason: null }] };
  return new OpenAI({ apiKey: 'synthetic-not-a-key', maxRetries: 0, fetch: async (_url, options) => {
    const encoder = new TextEncoder();
    const body = new ReadableStream({ start(controller) {
      controller.enqueue(encoder.encode(`data: ${JSON.stringify(chunk)}\n\n`));
      if (unfinished) options.signal.addEventListener('abort', () => controller.error(new DOMException('Synthetic cancellation', 'AbortError')), { once: true });
      else {
        if (usage) {
          const terminal = { ...chunk, choices: [{ index: 0, ...(operation === 'chat' ? { delta: {} } : { text: '' }), finish_reason: 'stop' }] };
          controller.enqueue(encoder.encode(`data: ${JSON.stringify(terminal)}\n\n`));
          controller.enqueue(encoder.encode(`data: ${JSON.stringify({ ...chunk, choices: [], usage: { prompt_tokens: 12, completion_tokens: 7, total_tokens: 19 } })}\n\n`));
        }
        controller.enqueue(encoder.encode('data: [DONE]\n\n')); controller.close();
      }
    } });
    return new Response(body, { headers: { 'content-type': 'text/event-stream' } });
  } });
}
function callStream(c, operation) {
  return operation === 'chat'
    ? c.chat.completions.create({ model: 'requested-model', messages: [], stream: true, stream_options: { include_usage: true } })
    : c.completions.create({ model: 'requested-model', prompt: 'synthetic', stream: true, stream_options: { include_usage: true } });
}
test(`${sdkVersion}: baseline normal promise helpers and disable`, async () => {
  instrument(); const c = client(chat); const p = c.chat.completions.create({ model: 'synthetic', messages: [] });
  assert.equal((await p.withResponse()).data, await p); assert.equal(span().status.code, 1);
  instrumentation.disable(); await c.chat.completions.create({ model: 'synthetic', messages: [] }); assert.equal(exporter.getFinishedSpans().length, 1);
});
for (const operation of ['chat', 'completions']) {
  test(`${sdkVersion}: ${operation} controller abort is not successful`, async () => {
    instrument(); const stream = await callStream(streamingClient(operation, { unfinished: true }), operation);
    let chunks = 0; for await (const chunk of stream) { chunks++; stream.controller.abort(); }
    assert.equal(chunks, 1); assert.notEqual(span().status.code, 1, 'aborted request must not be explicitly OK');
  });
  test(`${sdkVersion}: ${operation} early exit is not successful`, async () => {
    instrument(); const stream = await callStream(streamingClient(operation, { unfinished: true }), operation);
    for await (const chunk of stream) break;
    assert.notEqual(span().status.code, 1, 'early exit aborted request must not be explicitly OK');
  });
  test(`${sdkVersion}: ${operation} abort before iteration finishes span`, async () => {
    instrument(); const stream = await callStream(streamingClient(operation, { unfinished: true }), operation);
    stream.controller.abort(); stream.controller.abort(); await tick(); await provider.forceFlush();
    // Starting an already aborted iterator must neither finish twice nor turn it OK.
    for await (const chunk of stream) {}
    assert.equal(exporter.getFinishedSpans().length, 1, 'a closed/aborted stream must finish even without first next()');
  });
  test(`${sdkVersion}: ${operation} streaming usage survives aggregation`, async () => {
    instrument(); for await (const chunk of await callStream(streamingClient(operation, { usage: true }), operation)) {}
    assert.equal(span().attributes['gen_ai.usage.input_tokens'], 12);
    assert.equal(span().attributes['gen_ai.usage.output_tokens'], 7);
    assert.equal(span().attributes['gen_ai.usage.total_tokens'], 19);
    assert.equal(span().attributes['gen_ai.response.model'], 'returned-model');
    assert.equal(span().attributes['gen_ai.response.id'], operation === 'chat' ? 'chat-synthetic' : 'cmpl-synthetic');
    assert.deepEqual(JSON.parse(span().attributes['gen_ai.response.finish_reasons']), ['stop']);
    assert.equal(span().status.code, 1);
  });
}
if (OpenAI.Responses) {
  test(`${sdkVersion}: valid stored-prompt response without input reaches transport`, async () => {
    const body = { model: 'synthetic', prompt: { id: 'pmpt_synthetic' } }; const metrics = {}; const c = client(response, metrics);
    assert.equal((await c.responses.create(body)).id, response.id); assert.equal(metrics.requests, 1);
    instrument(); assert.equal((await c.responses.create(body)).id, response.id); assert.equal(metrics.requests, 2);
  });
  test(`${sdkVersion}: custom tool output does not reject a successful SDK response`, async () => {
    const fixture = { ...chat, choices: [{ index: 0, finish_reason: 'tool_calls', message: { role: 'assistant', content: null, tool_calls: [toolCall] } }] };
    const c = client(fixture); const args = { model: 'synthetic', messages: [], tools: [{ type: 'custom', custom: { name: 'code_exec' } }] };
    assert.equal((await c.chat.completions.create(args)).choices[0].message.tool_calls[0].type, 'custom');
    instrument(); const promise = c.chat.completions.create(args);
    const actual = await promise;
    assert.equal((await promise.withResponse()).data, actual);
    assert.deepEqual(actual.choices[0].message.tool_calls[0], toolCall);
    assert.deepEqual(JSON.parse(span().attributes['gen_ai.output.messages'])[0].tool_calls[0], toolCall);
  });
  test(`${sdkVersion}: custom tool history does not block the next request`, async () => {
    const c = client(chat); const args = { model: 'synthetic', messages: [{ role: 'assistant', content: null, tool_calls: [toolCall] }, { role: 'tool', tool_call_id: toolCall.id, content: '1' }] };
    await c.chat.completions.create(args); instrument(); await c.chat.completions.create(args);
    assert.deepEqual(JSON.parse(span().attributes['gen_ai.input.messages'])[0].tool_calls[0], toolCall);
  });
  test(`${sdkVersion}: terminal failed response is not OK`, async () => {
    instrument(); const fixture = { ...response, status: 'failed', error: { code: 'server_error', message: 'synthetic failure' } };
    assert.equal((await client(fixture).responses.create({ model: 'synthetic', input: 'synthetic' })).status, 'failed');
    assert.equal(span().status.code, 2, 'failed response must be ERROR');
  });
}
test(`${sdkVersion}: FI_HIDE_INPUTS covers embedding text attributes`, async () => {
  process.env.FI_HIDE_INPUTS = 'true';
  try {
    instrument(); await client({ object: 'list', data: [{ index: 0, embedding: [0.125, 0.875] }], model: 'synthetic' }).embeddings.create({ model: 'synthetic', input: 'SYNTHETIC-PRIVATE-EMBEDDING', encoding_format: 'float' });
    assert.ok(!JSON.stringify(span().attributes).includes('SYNTHETIC-PRIVATE-EMBEDDING'), 'hidden embedding input leaked');
  } finally { delete process.env.FI_HIDE_INPUTS; }
});
test(`${sdkVersion}: FI_HIDE_EMBEDDING_VECTORS covers raw output`, async () => {
  process.env.FI_HIDE_EMBEDDING_VECTORS = 'true';
  try {
    instrument(); await client({ object: 'list', data: [{ index: 0, embedding: [0.125, 0.875] }], model: 'synthetic' }).embeddings.create({ model: 'synthetic', input: 'synthetic', encoding_format: 'float' });
    assert.ok(!JSON.stringify(span().attributes).includes('0.875'), 'hidden embedding vector leaked through raw.output');
  } finally { delete process.env.FI_HIDE_EMBEDDING_VECTORS; }
});

for (const operation of ['chat', 'completions']) {
  test(`${sdkVersion}: ${operation} request deadline is recorded as failure`, async () => {
    instrument(); const c = streamingClient(operation, { unfinished: true });
    const controller = new AbortController();
    const stream = await (operation === 'chat'
      ? c.chat.completions.create({ model: 'synthetic', messages: [], stream: true }, { signal: controller.signal })
      : c.completions.create({ model: 'synthetic', prompt: 'synthetic', stream: true }, { signal: controller.signal }));
    const listenersDuring = getEventListeners(controller.signal, 'abort').length;
    let chunks = 0;
    for await (const chunk of stream) {
      chunks++;
      controller.abort(new DOMException('Synthetic deadline exceeded', 'TimeoutError'));
    }
    // The real SDK swallows AbortError. Instrumentation must not introduce a rejection.
    assert.equal(chunks, 1);
    assert.equal(span().status.code, 2, 'timed-out operation must be ERROR');
    assert.equal(span().events.length, 1);
    assert.equal(span().events[0].attributes['exception.type'], 'TimeoutError');
    // SDK 4 retains its own abort forwarder; only our listener must be removed.
    assert.ok(getEventListeners(controller.signal, 'abort').length < listenersDuring);
  });
  test(`${sdkVersion}: ${operation} request deadline before first next ends exactly once`, async () => {
    instrument({ hideErrorDetails: true });
    const c = streamingClient(operation, { unfinished: true });
    const controller = new AbortController();
    const stream = await (operation === 'chat'
      ? c.chat.completions.create({ model: 'synthetic', messages: [], stream: true }, { signal: controller.signal })
      : c.completions.create({ model: 'synthetic', prompt: 'synthetic', stream: true }, { signal: controller.signal }));
    const listenersDuring = getEventListeners(controller.signal, 'abort').length;
    controller.abort(new DOMException('SYNTHETIC-PRIVATE-DEADLINE', 'TimeoutError'));
    await tick();
    assert.equal(span().status.code, 2);
    for await (const chunk of stream) {}
    stream.controller.abort();
    const finished = span();
    assert.ok(!JSON.stringify({ attrs: finished.attributes, status: finished.status, events: finished.events }).includes('SYNTHETIC-PRIVATE-DEADLINE'));
    // SDK 4 retains its own abort forwarder; only our listener must be removed.
    assert.ok(getEventListeners(controller.signal, 'abort').length < listenersDuring);
  });
  test(`${sdkVersion}: ${operation} normal completion detaches request abort listener`, async () => {
    instrument(); const c = streamingClient(operation);
    const controller = new AbortController();
    const stream = await (operation === 'chat'
      ? c.chat.completions.create({ model: 'synthetic', messages: [], stream: true }, { signal: controller.signal })
      : c.completions.create({ model: 'synthetic', prompt: 'synthetic', stream: true }, { signal: controller.signal }));
    const listenersDuring = getEventListeners(controller.signal, 'abort').length;
    for await (const chunk of stream) {}
    assert.ok(getEventListeners(controller.signal, 'abort').length < listenersDuring);
    assert.equal(span().status.code, 1);
    controller.abort(new DOMException('Late abort must not change success', 'TimeoutError'));
    assert.equal(span().status.code, 1);
  });
}
if (OpenAI.Responses) {
  for (const status of ['failed', 'incomplete']) {
    for (const streamed of [false, true]) {
      test(`${sdkVersion}: Responses ${status} streamed=${streamed} keeps SDK payload and hidden errors`, async () => {
        const fixture = { ...response, status, error: status === 'failed' ? { code: 'server_error', message: 'SYNTHETIC-PRIVATE-FAILURE' } : null };
        const c = new OpenAI({ apiKey: 'synthetic', maxRetries: 0, fetch: async () => new Response(
          streamed ? `data: ${JSON.stringify({ type: `response.${status}`, response: fixture })}\n\ndata: [DONE]\n\n` : JSON.stringify(fixture),
          { headers: { 'content-type': streamed ? 'text/event-stream' : 'application/json' } },
        ) });
        const consume = async () => {
          const result = await c.responses.create({ model: 'synthetic', input: 'synthetic', stream: streamed });
          if (!streamed) return result;
          const events = []; for await (const event of result) events.push(event);
          return events;
        };
        const baseline = await consume();
        instrument({ hideErrorDetails: true });
        assert.deepEqual(await consume(), baseline);
        const finished = span();
        assert.equal(finished.status.code, 2);
        assert.ok(!JSON.stringify({ attrs: finished.attributes, status: finished.status, events: finished.events }).includes('SYNTHETIC-PRIVATE-FAILURE'));
      });
    }
  }
}
