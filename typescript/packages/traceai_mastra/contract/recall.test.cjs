// Offline contract: real Mastra lifecycles and decoded OTLP/protobuf, synthetic data only.
// Run against the peer floor and latest in separate dependency installations:
// MASTRA_TEST_DEPS=/path/to/install MASTRA_TEST_PACKAGE=/path/to/built/index.js node --test contract/recall.test.cjs
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const { createRequire } = require('node:module');
const { test, before, after, beforeEach, afterEach } = require('node:test');
const packagePath = process.env.MASTRA_TEST_PACKAGE || path.resolve(__dirname, '../dist/src/index.js');
const req = createRequire(process.env.MASTRA_TEST_DEPS
  ? path.join(process.env.MASTRA_TEST_DEPS, 'package.json') : packagePath);
const { createFIObservability } = require(packagePath);
const { SpanType } = req('@mastra/core/observability');
const wire = req('@opentelemetry/otlp-transformer/build/src/generated/root.js')
  .opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
const flags = ['INPUTS', 'OUTPUTS', 'INPUT_MESSAGES', 'OUTPUT_MESSAGES', 'INPUT_TEXT',
  'OUTPUT_TEXT', 'INPUT_IMAGES', 'EMBEDDING_VECTORS', 'ERROR_DETAILS'].map(key => `FI_HIDE_${key}`);
const envKeys = [...flags, 'FI_PROJECT_NAME'];
let originalEnv, obs, endpoint;
const spans = [];
const attributes = list => Object.fromEntries((list || []).map(a => [a.key, Object.values(a.value)[0]]));
const server = http.createServer((request, response) => {
  const chunks = [];
  request.on('data', chunk => chunks.push(chunk));
  request.on('end', () => {
    assert.equal(request.url, '/tracer/v1/traces');
    assert.equal(request.headers['x-api-key'], 'synthetic-api');
    assert.equal(request.headers['x-secret-key'], 'synthetic-secret');
    const data = wire.toObject(wire.decode(Buffer.concat(chunks)), { longs: String, bytes: String });
    for (const resource of data.resourceSpans || []) {
      for (const scope of resource.scopeSpans || []) {
        for (const span of scope.spans || []) spans.push({ ...span, resource: attributes(resource.resource.attributes), attrs: attributes(span.attributes) });
      }
    }
    response.writeHead(200, { 'content-type': 'application/x-protobuf' });
    response.end();
  });
});
before(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  endpoint = `http://127.0.0.1:${server.address().port}/tracer/v1/traces`;
});
after(() => new Promise(resolve => server.close(resolve)));
beforeEach(() => {
  originalEnv = Object.fromEntries(envKeys.map(key => [key, process.env[key]]));
  for (const key of envKeys) delete process.env[key];
  spans.length = 0;
});
afterEach(async () => {
  await obs?.shutdown();
  obs = undefined;
  for (const [key, value] of Object.entries(originalEnv)) {
    if (value === undefined) delete process.env[key]; else process.env[key] = value;
  }
});
function setup(options = {}) {
  obs = createFIObservability({ serviceName: 'synthetic-service', apiKey: 'synthetic-api',
    secretKey: 'synthetic-secret', endpoint, ...options });
  return obs.getDefaultInstance();
}
async function flush() {
  // Wait only in content tests; lifecycle regression tests intentionally do not yield.
  await new Promise(resolve => setImmediate(resolve));
  await obs.getDefaultInstance().flush();
}
function privateSpans(instance) {
  const agent = instance.startSpan({ type: SpanType.AGENT_RUN, name: 'privacy-agent',
    input: 'PRIVATE_INPUT', attributes: { instructions: 'PRIVATE_SYSTEM' } });
  const tool = instance.startSpan({ type: SpanType.TOOL_CALL, name: 'privacy-tool', parent: agent,
    entityName: 'tool', input: { apiKey: 'PRIVATE_CREDENTIAL', prompt: 'PRIVATE_INPUT' } });
  tool.end({ output: 'PRIVATE_OUTPUT' });
  const model = instance.startSpan({ type: SpanType.MODEL_INFERENCE || SpanType.MODEL_GENERATION,
    name: 'privacy-model', parent: agent, attributes: { model: 'synthetic', usage: { inputTokens: 3, outputTokens: 2 } },
    input: [{ role: 'user', content: [{ type: 'text', text: 'PRIVATE_INPUT' }, { type: 'image', image: 'PRIVATE_IMAGE' }] }] });
  model.end({ output: [{ role: 'assistant', content: 'PRIVATE_OUTPUT' }] });
  const error = instance.startSpan({ type: SpanType.TOOL_CALL, name: 'privacy-error', parent: agent, entityName: 'error' });
  error.error({ error: new Error('PRIVATE_ERROR'), endSpan: true });
  agent.end({ output: 'PRIVATE_OUTPUT' });
}
for (const flag of ['INPUTS', 'INPUT_MESSAGES', 'INPUT_TEXT']) {
  test(`FI_HIDE_${flag} protects native and FI input aliases and instructions`, async () => {
    process.env[`FI_HIDE_${flag}`] = 'true';
    privateSpans(setup());
    await flush();
    assert.equal(spans.length, 4);
    assert.doesNotMatch(JSON.stringify(spans), /PRIVATE_INPUT|PRIVATE_SYSTEM|PRIVATE_ERROR/);
    assert.match(JSON.stringify(spans), /PRIVATE_OUTPUT/);
  });
}
for (const flag of ['OUTPUTS', 'OUTPUT_MESSAGES', 'OUTPUT_TEXT']) {
  test(`FI_HIDE_${flag} protects native and FI output aliases`, async () => {
    process.env[`FI_HIDE_${flag}`] = 'true';
    privateSpans(setup());
    await flush();
    assert.equal(spans.length, 4);
    assert.doesNotMatch(JSON.stringify(spans), /PRIVATE_OUTPUT|PRIVATE_ERROR/);
    assert.match(JSON.stringify(spans), /PRIVATE_INPUT/);
  });
}
test('all FI privacy flags keep structure and error classification, not private content', async () => {
  for (const key of flags) process.env[key] = 'true';
  privateSpans(setup());
  await flush();
  assert.equal(spans.length, 4);
  assert.doesNotMatch(JSON.stringify(spans), /PRIVATE_/);
  const root = spans.find(span => span.attrs['gen_ai.span.kind'] === 'AGENT');
  assert.ok(root);
  assert.equal(new Set(spans.map(span => span.spanId)).size, 4);
  for (const span of spans.filter(span => span !== root)) {
    assert.equal(span.parentSpanId, root.spanId);
    assert.equal(span.traceId, root.traceId);
  }
  const error = spans.find(span => span.status?.code === 2);
  assert.ok(error);
  assert.ok(error.attrs['error.type']);
  assert.ok(error.events.some(event => attributes(event.attributes)['exception.type']));
});
test('FI_HIDE_ERROR_DETAILS alone hides errors without hiding input or output', async () => {
  process.env.FI_HIDE_ERROR_DETAILS = 'true';
  privateSpans(setup());
  await flush();
  assert.doesNotMatch(JSON.stringify(spans), /PRIVATE_ERROR/);
  assert.match(JSON.stringify(spans), /PRIVATE_INPUT/);
  assert.match(JSON.stringify(spans), /PRIVATE_OUTPUT/);
});
test('credentials are removed even with content hiding off', async () => {
  privateSpans(setup());
  await flush();
  assert.doesNotMatch(JSON.stringify(spans), /PRIVATE_CREDENTIAL/);
  assert.match(JSON.stringify(spans), /PRIVATE_INPUT|PRIVATE_OUTPUT/);
});
test('explicit error-detail false overrides implied hiding', async () => {
  process.env.FI_HIDE_INPUTS = 'true';
  process.env.FI_HIDE_ERROR_DETAILS = 'false';
  privateSpans(setup());
  await flush();
  assert.doesNotMatch(JSON.stringify(spans), /PRIVATE_INPUT/);
  assert.match(JSON.stringify(spans), /PRIVATE_ERROR/);
});
for (const [label, explicit, env, expected] of [
  ['environment', undefined, 'env-project', 'env-project'],
  ['explicit', 'option-project', 'env-project', 'option-project'],
  ['fallback', undefined, undefined, 'synthetic-service'],
]) {
  test(`project precedence: ${label}`, async () => {
    if (env) process.env.FI_PROJECT_NAME = env;
    const instance = setup({ projectName: explicit });
    instance.startSpan({ type: SpanType.GENERIC, name: 'project' }).end();
    await flush();
    assert.equal(spans.length, 1);
    assert.equal(spans[0].resource.project_name, expected);
  });
}
for (const lifecycle of ['flush', 'shutdown']) {
  test(`immediate ${lifecycle} drains all conversions exactly once`, async () => {
    const instance = setup();
    for (let i = 0; i < 8; i++) instance.startSpan({ type: SpanType.GENERIC, name: `span-${i}` }).end();
    await instance[lifecycle]();
    assert.equal(spans.length, 8);
    assert.equal(new Set(spans.map(span => span.spanId)).size, 8);
    if (lifecycle === 'shutdown') await instance.shutdown();
    assert.equal(spans.length, 8);
  });
}
test('generation produces one LLM observation per real model request', async () => {
  const { Mastra } = req('@mastra/core');
  const { Agent } = req('@mastra/core/agent');
  setup();
  let calls = 0;
  const model = { specificationVersion: 'v2', provider: 'synthetic', modelId: 'synthetic', supportedUrls: {},
    doGenerate: async () => { calls++; return { content: [{ type: 'text', text: 'answer' }], finishReason: 'stop',
      usage: { inputTokens: 11, outputTokens: 5, totalTokens: 16 }, warnings: [] }; } };
  const agent = new Agent({ id: 'test-agent', name: 'Test agent', instructions: 'Answer', model });
  const mastra = new Mastra({ agents: { agent }, observability: obs });
  const result = await mastra.getAgent('agent').generate('question');
  assert.equal(result.text, 'answer');
  await flush();
  assert.equal(calls, 1);
  const llm = spans.filter(span => span.attrs['gen_ai.span.kind'] === 'LLM');
  assert.equal(llm.length, calls);
  assert.equal(llm[0].status?.code, 1);
  assert.equal(Number(llm[0].attrs['gen_ai.usage.input_tokens']), 11);
  assert.equal(Number(llm[0].attrs['gen_ai.usage.output_tokens']), 5);
});
test('shutdown waits for delayed conversion and rejects new events', async () => {
  const instance = setup();
  const exporter = instance.getExporters()[0];
  const convert = exporter.converter.convertSpan.bind(exporter.converter);
  let release, entered;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  exporter.converter.convertSpan = async span => { entered(); await gate; return convert(span); };
  instance.startSpan({ type: SpanType.GENERIC, name: 'delayed' }).end();
  await started;
  let done = false;
  const shutdown = exporter.shutdown().then(() => { done = true; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(done, false);
  instance.startSpan({ type: SpanType.GENERIC, name: 'too-late' }).end();
  release();
  await shutdown;
  assert.equal(spans.length, 1);
  assert.equal(spans[0].name, 'delayed');
});
test('failed conversions settle and do not prevent draining healthy spans', async () => {
  const instance = setup();
  const exporter = instance.getExporters()[0];
  const convert = exporter.converter.convertSpan.bind(exporter.converter);
  exporter.converter.convertSpan = async span => {
    if (span.name === 'invalid') throw new Error('synthetic conversion failure');
    return convert(span);
  };
  instance.startSpan({ type: SpanType.GENERIC, name: 'invalid' }).end();
  instance.startSpan({ type: SpanType.GENERIC, name: 'valid' }).end();
  await instance.shutdown();
  assert.equal(spans.length, 1);
  assert.equal(spans[0].name, 'valid');
});
test('real stream cancellation preserves the unsuccessful SDK outcomes', { skip: !SpanType.MODEL_INFERENCE }, async () => {
  const { Mastra } = req('@mastra/core');
  const { Agent } = req('@mastra/core/agent');
  setup();
  const controller = new AbortController();
  const model = { specificationVersion: 'v2', provider: 'synthetic', modelId: 'synthetic', supportedUrls: {},
    doStream: async options => ({ stream: new ReadableStream({ start(stream) {
      stream.enqueue({ type: 'stream-start', warnings: [] });
      stream.enqueue({ type: 'text-start', id: 'text' });
      stream.enqueue({ type: 'text-delta', id: 'text', delta: 'partial' });
      options.abortSignal.addEventListener('abort', () => stream.error(new DOMException('caller abort', 'AbortError')), { once: true });
    } }) }) };
  const agent = new Agent({ id: 'cancel-agent', name: 'Cancel agent', instructions: 'Answer', model });
  const mastra = new Mastra({ agents: { agent }, observability: obs });
  const result = await mastra.getAgent('agent').stream('question', { abortSignal: controller.signal });
  const watchdog = setTimeout(() => controller.abort(), 3000);
  let sawText = false;
  try {
    for await (const chunk of result.fullStream) if (chunk.type === 'text-delta') { sawText = true; controller.abort(); }
  } catch (error) {
    assert.equal(controller.signal.aborted, true);
  } finally { clearTimeout(watchdog); }
  await flush();
  assert.equal(sawText, true);
  const cancelled = spans.filter(span => ['abort', 'interrupted'].includes(span.attrs['mastra.span.outcome']));
  assert.ok(cancelled.length >= 3);
  for (const span of cancelled) {
    assert.notEqual(span.status?.code, 1);
    assert.equal(span.attrs['mastra.span.stop_reason'], 'caller_cancelled');
  }
});
test('native unsuccessful outcomes are never OK', { skip: !SpanType.MODEL_INFERENCE }, async () => {
  const instance = setup();
  for (const termination of [
    { outcome: 'abort', stopReason: 'caller_cancelled' },
    { outcome: 'abort', stopReason: 'timeout' },
    { outcome: 'error', stopReason: 'runtime_error' },
    { outcome: 'interrupted', stopReason: 'signal_received' },
  ]) instance.startSpan({ type: SpanType.WORKFLOW_RUN, name: termination.stopReason }).end(termination);
  await flush();
  assert.equal(spans.length, 4);
  for (const span of spans) {
    assert.notEqual(span.status?.code, 1);
    assert.ok(span.attrs['mastra.span.outcome']);
    assert.ok(span.attrs['mastra.span.stop_reason']);
  }
});
