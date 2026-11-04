// Real Agent + OpenAI SDK against loopback HTTP/SSE, with decoded OTLP output.
// No provider credentials or vendor services are used.
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const { test, before, after, beforeEach, afterEach } = require('node:test');
const packagePath = process.env.MASTRA_TEST_PACKAGE || path.resolve(__dirname, '../dist/src/index.js');
const req = createRequire(process.env.MASTRA_TEST_DEPS
  ? path.join(process.env.MASTRA_TEST_DEPS, 'package.json') : packagePath);
const { Mastra } = req('@mastra/core');
const { Agent } = req('@mastra/core/agent');
const { observabilityFeatures } = req('@mastra/observability');
const { createOpenAI } = req('@ai-sdk/openai');
const transformer = createRequire(req.resolve('@opentelemetry/exporter-trace-otlp-proto'));
const wire = transformer('@opentelemetry/otlp-transformer/build/src/generated/root.js')
  .opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
const currentRuntime = observabilityFeatures?.has('model-inference-span') === true;
const attrs = list => Object.fromEntries((list || []).map(a => [a.key, Object.values(a.value)[0]]));
let createFIObservability, obs, agent, mode, requests, spans, ended;
const collector = http.createServer((request, response) => {
  const chunks = [];
  request.on('data', chunk => chunks.push(chunk));
  request.on('end', () => {
    assert.equal(request.headers['x-api-key'], 'synthetic-fi-key');
    const data = wire.toObject(wire.decode(Buffer.concat(chunks)), { longs: String, bytes: String });
    for (const resource of data.resourceSpans || []) for (const scope of resource.scopeSpans || []) {
      for (const span of scope.spans || []) spans.push({ ...span, attrs: attrs(span.attributes) });
    }
    response.writeHead(200, { 'content-type': 'application/x-protobuf' });
    response.end();
  });
});
const provider = http.createServer((request, response) => {
  let body = '';
  request.on('data', chunk => { body += chunk; });
  request.on('end', () => {
    const input = JSON.parse(body);
    requests.push(input);
    assert.equal(request.url, '/v1/chat/completions');
    if (mode === 'error') {
      response.writeHead(400, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ error: { message: 'SYNTHETIC_PROVIDER_ERROR', type: 'invalid_request_error' } }));
      return;
    }
    const common = { id: 'chatcmpl-synthetic', created: 1700000000, model: 'gpt-4o-mini' };
    const usage = { prompt_tokens: 11, completion_tokens: 5, total_tokens: 16 };
    if (!input.stream) {
      response.writeHead(200, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ ...common, object: 'chat.completion', usage,
        choices: [{ index: 0, message: { role: 'assistant', content: 'SYNTHETIC_RESPONSE' }, finish_reason: 'stop' }] }));
      return;
    }
    response.writeHead(200, { 'content-type': 'text/event-stream' });
    const send = value => response.write(`data: ${JSON.stringify({ ...common, object: 'chat.completion.chunk', ...value })}\n\n`);
    send({ choices: [{ index: 0, delta: { role: 'assistant', content: 'SYNTHETIC_STREAM' }, finish_reason: null }] });
    if (mode === 'abort') return; // Remain open until the real client cancels.
    send({ choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] });
    send({ choices: [], usage });
    response.end('data: [DONE]\n\n');
  });
});
const listen = server => new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
before(async () => {
  ({ createFIObservability } = await import(pathToFileURL(packagePath).href));
  await listen(collector);
  await listen(provider);
});
after(async () => {
  for (const server of [provider, collector]) {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
});
beforeEach(() => {
  mode = 'normal'; requests = []; spans = []; ended = [];
  obs = createFIObservability({ apiKey: 'synthetic-fi-key', secretKey: 'synthetic-fi-secret',
    serviceName: 'http-contract', endpoint: `http://127.0.0.1:${collector.address().port}/tracer/v1/traces` });
  const exporter = obs.getDefaultInstance().getExporters()[0];
  const exportEvent = exporter.exportTracingEvent.bind(exporter);
  exporter.exportTracingEvent = event => {
    if (event.type === 'span_ended') ended.push(event.exportedSpan);
    return exportEvent(event);
  };
  const openai = createOpenAI({ apiKey: 'synthetic-provider-key', baseURL: `http://127.0.0.1:${provider.address().port}/v1` });
  const instance = new Agent({ id: 'http-agent', name: 'HTTP agent', instructions: 'Synthetic instruction', model: openai.chat('gpt-4o-mini') });
  const mastra = new Mastra({ agents: { agent: instance }, observability: obs });
  agent = mastra.getAgent('agent');
});
afterEach(async () => { await obs.shutdown(); });
async function flush() { await obs.getDefaultInstance().flush(); }
function checkCall() {
  assert.equal(requests.length, 1);
  const llm = spans.filter(span => span.attrs['gen_ai.span.kind'] === 'LLM');
  assert.equal(llm.length, 1, 'one HTTP call must produce exactly one LLM observation');
  assert.equal(llm[0].attrs['gen_ai.request.model'], 'gpt-4o-mini');
  assert.match(JSON.stringify(llm[0].attrs), /openai/);
  assert.equal(Number(llm[0].attrs['gen_ai.usage.input_tokens']), 11);
  assert.equal(Number(llm[0].attrs['gen_ai.usage.output_tokens']), 5);
  assert.equal(spans.reduce((total, span) => total + Number(span.attrs['gen_ai.usage.input_tokens'] || 0), 0), 11);
  assert.equal(spans.reduce((total, span) => total + Number(span.attrs['gen_ai.usage.output_tokens'] || 0), 0), 5);
  assert.equal(new Set(spans.map(span => span.spanId)).size, spans.length);
  for (const child of spans.filter(span => span.parentSpanId)) {
    assert.ok(spans.some(parent => parent.spanId === child.parentSpanId && parent.traceId === child.traceId));
  }
}
test('real HTTP generation retains a single model call, usage, I/O and parent tree', async () => {
  assert.equal((await agent.generate('SYNTHETIC_PROMPT')).text, 'SYNTHETIC_RESPONSE');
  await flush();
  checkCall();
  assert.match(JSON.stringify(spans), /SYNTHETIC_PROMPT/);
  assert.match(JSON.stringify(spans), /SYNTHETIC_RESPONSE/);
});
test('real completed SSE stream retains a single model call and usage', async () => {
  const result = await agent.stream('SYNTHETIC_PROMPT');
  let text = '';
  for await (const part of result.textStream) text += part;
  assert.equal(text, 'SYNTHETIC_STREAM');
  await flush();
  checkCall();
});
test('real SSE cancellation preserves every native outcome on the wire', {
  skip: !currentRuntime && 'Mastra observability 1.0 does not emit cancellation outcomes', timeout: 10000,
}, async () => {
  mode = 'abort';
  const controller = new AbortController();
  const watchdog = setTimeout(() => controller.abort(), 5000);
  let sawText = false;
  try {
    const result = await agent.stream('SYNTHETIC_PROMPT', { abortSignal: controller.signal });
    for await (const part of result.fullStream) if (part.type === 'text-delta') { sawText = true; controller.abort(); }
  } catch (error) {
    assert.equal(controller.signal.aborted, true);
  } finally { clearTimeout(watchdog); }
  assert.equal(sawText, true);
  await flush();
  const native = ended.filter(span => ['abort', 'interrupted'].includes(span.outcome));
  assert.ok(native.length >= 3);
  for (const span of native) {
    const wireSpan = spans.find(item => item.spanId === Buffer.from(span.id, 'hex').toString('base64'));
    assert.ok(wireSpan, `missing cancelled span ${span.name}`);
    assert.notEqual(wireSpan.status?.code, 1);
    assert.equal(wireSpan.attrs['mastra.span.outcome'], span.outcome);
    assert.equal(wireSpan.attrs['mastra.span.stop_reason'], 'caller_cancelled');
  }
});
test('real provider failure is not exported as success', {
  skip: !currentRuntime && 'Mastra observability 1.0 omits the real Agent provider-error outcome',
}, async () => {
  mode = 'error';
  await assert.rejects(agent.generate('SYNTHETIC_PROMPT'));
  await flush();
  assert.ok(spans.some(span => span.status?.code === 2));
});
