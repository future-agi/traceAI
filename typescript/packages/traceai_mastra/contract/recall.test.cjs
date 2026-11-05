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
const { pathToFileURL } = require('node:url');
let createFIObservability;
const { SpanType } = req('@mastra/core/observability');
// Core's enum alone is not evidence that the installed observability runtime
// implements outcomes. In particular, new-core/old-observability is supported.
const { observabilityFeatures } = req('@mastra/observability');
const supportsOutcomes = observabilityFeatures?.has('model-inference-span') === true;

const wire = req('@opentelemetry/otlp-transformer/build/src/generated/root.js')
  .opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
const flags = ['INPUTS', 'OUTPUTS', 'INPUT_MESSAGES', 'OUTPUT_MESSAGES', 'INPUT_TEXT',
  'OUTPUT_TEXT', 'INPUT_IMAGES', 'EMBEDDING_VECTORS', 'ERROR_DETAILS'].map(key => `FI_HIDE_${key}`);
const envKeys = [...flags, 'FI_PROJECT_NAME', 'FI_PII_REDACTION'];
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
        for (const span of scope.spans || []) spans.push({ ...span, scope: scope.scope, resource: attributes(resource.resource.attributes), attrs: attributes(span.attributes) });
      }
    }
    response.writeHead(200, { 'content-type': 'application/x-protobuf' });
    response.end();
  });
});
before(async () => {
  ({ createFIObservability } = await import(pathToFileURL(packagePath).href));
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
    await obs[lifecycle]();
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
  assert.equal(spans.filter(span => span.attrs['gen_ai.operation.name'] === 'chat').length, calls);
  assert.equal(spans.reduce((n, span) => n + Number(span.attrs['gen_ai.usage.input_tokens'] || 0), 0), 11);
  assert.equal(spans.reduce((n, span) => n + Number(span.attrs['gen_ai.usage.output_tokens'] || 0), 0), 5);
  assert.equal(llm[0].status?.code, 1);
  assert.equal(Number(llm[0].attrs['gen_ai.usage.input_tokens']), 11);
  assert.equal(Number(llm[0].attrs['gen_ai.usage.output_tokens']), 5);
});
test('real agent tool execution classifies processor_run as CHAIN', async () => {
  const { Mastra } = req('@mastra/core');
  const { Agent } = req('@mastra/core/agent');
  const { createTool } = req('@mastra/core/tools');
  const { z } = req('zod');
  setup();
  let calls = 0;
  const model = { specificationVersion: 'v2', provider: 'synthetic', modelId: 'synthetic', supportedUrls: {},
    doGenerate: async () => ++calls === 1
      ? { content: [{ type: 'tool-call', toolCallId: 'tool-1', toolName: 'lookup', input: '{}' }],
        finishReason: 'tool-calls', usage: { inputTokens: 2, outputTokens: 1 }, warnings: [] }
      : { content: [{ type: 'text', text: 'done' }], finishReason: 'stop',
        usage: { inputTokens: 2, outputTokens: 1 }, warnings: [] } };
  const lookup = createTool({ id: 'lookup', description: 'Look up synthetic data',
    inputSchema: z.object({}), execute: async () => ({ ok: true }) });
  const agent = new Agent({ id: 'processor-agent', name: 'Processor agent', instructions: 'Use tool', model, tools: { lookup } });
  const mastra = new Mastra({ agents: { agent }, observability: obs });
  await mastra.getAgent('agent').generate('Run lookup', { maxSteps: 3 });
  await flush();
  const processors = spans.filter(span => span.attrs['mastra.span.type'] === 'processor_run');
  if (supportsOutcomes) assert.ok(processors.length > 0, 'current runtime must emit processor_run');
  for (const span of processors) assert.equal(span.attrs['gen_ai.span.kind'], 'CHAIN');
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
  try {
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(done, false);
    instance.startSpan({ type: SpanType.GENERIC, name: 'too-late' }).end();
  } finally {
    release();
    await shutdown;
  }
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
test('real stream cancellation preserves the unsuccessful SDK outcomes', { skip: !supportsOutcomes }, async () => {
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
test('native unsuccessful outcomes are never OK', { skip: !supportsOutcomes }, async () => {
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

// Round-one recall regressions: native converter aliases, not invented wire keys.
test('input hiding covers native stop sequences and tool descriptions', async () => {
  process.env.FI_HIDE_INPUTS = 'true';
  const instance = setup();
  instance.startSpan({ type: SpanType.MODEL_INFERENCE || SpanType.MODEL_GENERATION,
    name: 'native-model', input: 'PRIVATE_INPUT', attributes: {
      model: 'synthetic', parameters: { stopSequences: ['PRIVATE_STOP_SEQUENCE'] },
    } }).end({ output: 'safe-output' });
  instance.startSpan({ type: SpanType.TOOL_CALL, name: 'native-tool', entityName: 'tool',
    input: 'PRIVATE_INPUT', attributes: { toolDescription: 'PRIVATE_TOOL_DESCRIPTION' },
  }).end({ output: 'safe-output' });
  await flush();
  assert.equal(spans.length, 2);
  assert.doesNotMatch(JSON.stringify(spans), /PRIVATE_/);
});
test('input hiding covers stop sequences supplied to a real generation', async () => {
  process.env.FI_HIDE_INPUTS = 'true';
  const { Mastra } = req('@mastra/core');
  const { Agent } = req('@mastra/core/agent');
  setup();
  let calls = 0;
  const model = { specificationVersion: 'v2', provider: 'synthetic', modelId: 'synthetic', supportedUrls: {},
    doGenerate: async options => {
      calls++;
      assert.deepEqual(options.stopSequences, ['PRIVATE_STOP_SEQUENCE']);
      return { content: [{ type: 'text', text: 'safe-output' }], finishReason: 'stop',
        usage: { inputTokens: 11, outputTokens: 5, totalTokens: 16 }, warnings: [] };
    } };
  const agent = new Agent({ id: 'stop-agent', name: 'Stop agent', instructions: 'PRIVATE_SYSTEM', model });
  const mastra = new Mastra({ agents: { agent }, observability: obs });
  await mastra.getAgent('agent').generate('PRIVATE_INPUT', { modelSettings: { stopSequences: ['PRIVATE_STOP_SEQUENCE'] } });
  await flush();
  assert.equal(calls, 1);
  assert.ok(spans.length > 0);
  assert.doesNotMatch(JSON.stringify(spans), /PRIVATE_/);
});
test('credential scrubbing covers nested camelCase keys without upstream filtering', async () => {
  const instance = setup();
  instance.startSpan({ type: SpanType.TOOL_CALL, name: 'credential-tool', entityName: 'tool',
    input: { clientSecret: 'PRIVATE_CLIENT_SECRET', privateKey: 'PRIVATE_PRIVATE_KEY',
      nested: [{ apiKey: 'PRIVATE_API_KEY', accessToken: 'PRIVATE_ACCESS_TOKEN', keep: 'safe' }] },
  }).end({ output: { privateKey: 'PRIVATE_OUTPUT_KEY', keep: 'safe' } });
  await flush();
  assert.equal(spans.length, 1);
  assert.doesNotMatch(JSON.stringify(spans), /PRIVATE_/);
  assert.match(JSON.stringify(spans), /safe/);
});
for (const errorText of [
  'Request failed; headers: {"Authorization":"Bearer PRIVATE_JSON_AUTH"}',
  'Authorization: Basic c3ludGhldGljOmNyZWRlbnRpYWw=',
  'Request failed; headers: {"proxy-authorization":"Basic c3ludGhldGljOmNyZWRlbnRpYWw="}',
  "Request failed; clientSecret='PRIVATE_CLIENT_SECRET' privateKey=PRIVATE_PRIVATE_KEY",
]) test(`scrubs credential-bearing errors: ${errorText}`, async () => {
  setup().startSpan({ type: SpanType.TOOL_CALL, name: 'credential-error', entityName: 'tool' })
    .error({ error: new Error(errorText), endSpan: true });
  await flush();
  assert.equal(spans.length, 1);
  assert.equal(spans[0].status.code, 2);
  assert.doesNotMatch(JSON.stringify(spans), /PRIVATE_|c3ludGhldGljOmNyZWRlbnRpYWw=/);
});
for (const enabled of [true, false]) test(`PII redaction is opt-in (${enabled}) across native/FI content and errors`, async () => {
  process.env.FI_PII_REDACTION = String(enabled);
  const instance = setup();
  instance.startSpan({ type: SpanType.TOOL_CALL, name: 'pii-tool', entityName: 'tool',
    input: { contact: 'customer@example.test', count: 7 },
  }).end({ output: ['customer@example.test', 'safe'] });
  instance.startSpan({ type: SpanType.TOOL_CALL, name: 'pii-error', entityName: 'tool' })
    .error({ error: new Error('Contact customer@example.test'), endSpan: true });
  await flush();
  assert.equal(spans.length, 2);
  const data = JSON.stringify(spans);
  if (enabled) {
    assert.doesNotMatch(data, /customer@example\.test/);
    assert.match(data, /<EMAIL_ADDRESS>/);
  } else assert.match(data, /customer@example\.test/);
});
test('integration version is deterministic on the OTLP resource and scope', async () => {
  setup().startSpan({ type: SpanType.GENERIC, name: 'version' }).end();
  await flush();
  const { VERSION } = await import(pathToFileURL(path.join(path.dirname(packagePath), 'version.js')).href);
  assert.equal(spans.length, 1);
  assert.equal(spans[0].scope.version, VERSION);
  assert.equal(spans[0].resource['telemetry.sdk.version'], VERSION);
});

// Round-two recall: credential serialization and older-converter context gaps.
test('AWS credentials never leave native tool I/O with content capture enabled', async () => {
  setup().startSpan({ type: SpanType.TOOL_CALL, name: 'aws-tool', input: {
    credentials: { accessKeyId: 'PRIVATE_AWS_ID', secretAccessKey: 'PRIVATE_AWS_SECRET', sessionToken: 'PRIVATE_AWS_SESSION' },
    keep: 'safe-input',
  } }).end({ output: { secret_access_key: 'PRIVATE_OUTPUT_SECRET', keep: 'safe-output' } });
  await flush();
  assert.equal(spans.length, 1);
  assert.doesNotMatch(JSON.stringify(spans), /PRIVATE_/);
  assert.match(spans[0].attrs['input.value'], /safe-input/);
  assert.match(spans[0].attrs['output.value'], /safe-output/);
});
for (const message of [
  'Request failed; Cookie: sessionid=PRIVATE_FIRST_COOKIE; sid=PRIVATE_SECOND_COOKIE',
  'Request failed; Set-Cookie: sid=PRIVATE_COOKIE; HttpOnly; Secure',
  'Request failed; encoded config: ' + JSON.stringify(JSON.stringify({ headers: { Authorization: 'Bearer PRIVATE_ESCAPED_AUTH' }, keep: 'safe' })),
  'Request failed; encoded config: ' + JSON.stringify(JSON.stringify(JSON.stringify({ credentials: { secretAccessKey: 'PRIVATE_ENCODED_SECRET' } }))),
  'Request failed; config: ' + JSON.stringify({ sessionToken: 'PRIVATE_SESSION', auth_token: 'PRIVATE_AUTH' }),
  'Request failed; Authorization: AWS4-HMAC-SHA256 Credential=PRIVATE_ACCESS/20261102/us-east-1/s3/aws4_request, SignedHeaders=host;x-amz-date, Signature=PRIVATE_SIGNATURE',
  'Request failed; Authorization: Digest username="synthetic", realm="test", nonce="PRIVATE_NONCE", response="PRIVATE_DIGEST"',
]) test(`serialized credentials are scrubbed in status and exception events: ${message}`, async () => {
  setup().startSpan({ type: SpanType.TOOL_CALL, name: 'serialized-error' }).error({ error: new Error(message), endSpan: true });
  await flush();
  assert.equal(spans.length, 1);
  assert.equal(spans[0].status.code, 2);
  assert.ok(spans[0].events.length > 0);
  assert.doesNotMatch(JSON.stringify(spans), /PRIVATE_/);
});
test('a real agent remains nested under its remote parent with every supported converter', async () => {
  const { Mastra } = req('@mastra/core');
  const { Agent } = req('@mastra/core/agent');
  setup();
  const model = { specificationVersion: 'v2', provider: 'synthetic', modelId: 'synthetic', supportedUrls: {},
    doGenerate: async () => ({ content: [{ type: 'text', text: 'safe' }], finishReason: 'stop',
      usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2 }, warnings: [] }) };
  const agent = new Agent({ id: 'remote', name: 'Remote agent', instructions: 'Answer', model });
  const mastra = new Mastra({ agents: { agent }, observability: obs });
  await mastra.getAgent('agent').generate('question', { tracingOptions: {
    traceId: '11111111111111111111111111111111', parentSpanId: '2222222222222222',
  } });
  await flush();
  const root = spans.find(span => span.attrs['gen_ai.span.kind'] === 'AGENT');
  assert.ok(root);
  assert.equal(root.traceId, Buffer.from('11111111111111111111111111111111', 'hex').toString('base64'));
  assert.equal(root.parentSpanId, Buffer.from('2222222222222222', 'hex').toString('base64'));
  assert.equal(new Set(spans.map(span => span.spanId)).size, spans.length);
});
test('native remote parentage and explicit links survive conversion without duplicates', async () => {
  const instance = setup();
  const root = instance.startSpan({ type: SpanType.AGENT_RUN, name: 'linked-agent',
    tracingOptions: { traceId: '11111111111111111111111111111111', parentSpanId: '2222222222222222' },
    links: [{ traceId: '33333333333333333333333333333333', spanId: '4444444444444444' }],
  });
  instance.startSpan({ type: SpanType.TOOL_CALL, name: 'local-child', parent: root }).end();
  root.end();
  await flush();
  const agent = spans.find(span => span.attrs['gen_ai.span.kind'] === 'AGENT');
  const child = spans.find(span => span.attrs['gen_ai.span.kind'] === 'TOOL');
  assert.equal(spans.length, 2);
  assert.equal(agent.parentSpanId, Buffer.from('2222222222222222', 'hex').toString('base64'));
  assert.equal(child.parentSpanId, agent.spanId);
  // The old runtime cannot produce links; old converters still must retain
  // links supplied by newer runtimes (the mixed row runs this assertion).
  if (supportsOutcomes) {
    assert.equal(agent.links.length, 1);
    assert.equal(agent.links[0].traceId, Buffer.from('33333333333333333333333333333333', 'hex').toString('base64'));
    assert.equal(agent.links[0].spanId, Buffer.from('4444444444444444', 'hex').toString('base64'));
  }
});
test('all native workflow control-flow kinds are classified as chains', async () => {
  const instance = setup();
  for (const type of [SpanType.WORKFLOW_SLEEP, SpanType.WORKFLOW_WAIT_EVENT, SpanType.WORKFLOW_CONDITIONAL_EVAL]) {
    assert.ok(type);
    instance.startSpan({ type, name: type }).end();
  }
  await flush();
  assert.equal(spans.length, 3);
  for (const span of spans) assert.equal(span.attrs['gen_ai.span.kind'], 'CHAIN');
});
test('a successful real workflow sleep is classified without duplicating its spans', async () => {
  const { Mastra } = req('@mastra/core');
  const { createWorkflow, createStep } = req('@mastra/core/workflows');
  const { z } = req('zod');
  setup();
  const schema = z.object({ q: z.string() });
  const step = createStep({ id: 'safe-step', inputSchema: schema, outputSchema: schema, execute: async ({ inputData }) => inputData });
  const workflow = createWorkflow({ id: 'sleep-workflow', inputSchema: schema, outputSchema: schema }).then(step).sleep(1).commit();
  const mastra = new Mastra({ workflows: { workflow }, observability: obs });
  const run = await mastra.getWorkflow('workflow').createRun();
  const result = await run.start({ inputData: { q: 'safe' } });
  assert.equal(result.status, 'success');
  await flush();
  const sleeps = spans.filter(span => span.attrs['mastra.span.type'] === 'workflow_sleep');
  assert.equal(sleeps.length, 1);
  for (const span of spans) assert.equal(span.attrs['gen_ai.span.kind'], 'CHAIN');
  assert.equal(new Set(spans.map(span => span.spanId)).size, spans.length);
});
test('an explicit native link survives even the oldest converter', { skip: !supportsOutcomes }, async () => {
  setup().startSpan({ type: SpanType.AGENT_RUN, name: 'linked-root',
    links: [{ traceId: '33333333333333333333333333333333', spanId: '4444444444444444' }],
  }).end();
  await flush();
  assert.equal(spans.length, 1);
  assert.equal(spans[0].links?.length, 1);
  assert.equal(spans[0].links[0].traceId, Buffer.from('33333333333333333333333333333333', 'hex').toString('base64'));
  assert.equal(spans[0].links[0].spanId, Buffer.from('4444444444444444', 'hex').toString('base64'));
});


// Native costContext is a query total on model_generation, unlike usage which
// belongs to model_inference on current runtimes. Check decoded numeric OTLP,
// including copies on child inference spans, so aggregate cost is not doubled.
for (const amount of [0, 0.1234567]) {
  test(`native provider cost ${amount} survives with one billable owner`, async () => {
    const instance = setup();
    const costContext = { estimatedCost: amount, costUnit: 'USD', costMetadata: { source: 'provider_reported' } };
    const generation = instance.startSpan({ type: 'model_generation', name: 'cost-generation',
      attributes: { model: 'synthetic', provider: 'openrouter', costContext } });
    if (supportsOutcomes) {
      const inference = instance.startSpan({ type: 'model_inference', name: 'cost-inference', parent: generation,
        attributes: { model: 'synthetic', provider: 'openrouter', costContext, usage: { inputTokens: 17, outputTokens: 9 } } });
      inference.end();
    }
    generation.end();
    await flush();
    assert.equal(spans.length, supportsOutcomes ? 2 : 1);
    const owners = spans.filter(span => span.attrs['gen_ai.cost.total'] !== undefined);
    assert.equal(owners.length, 1);
    assert.equal(Number(owners[0].attrs['gen_ai.cost.total']), amount);
    assert.equal(owners[0].attrs['mastra.span.type'], 'model_generation');
    for (const span of spans) {
      assert.equal(Number(span.attrs['mastra.cost.amount']), amount);
      assert.equal(span.attrs['mastra.cost.unit'], 'USD');
      assert.equal(span.attrs['mastra.cost.source'], 'provider_reported');
    }
  });
}
for (const amount of [NaN, Infinity, -Infinity, -1, '0.1', null, undefined]) {
  test(`invalid native cost ${String(amount)} is not exported`, async () => {
    const span = setup().startSpan({ type: 'model_generation', name: 'invalid-cost', attributes: {
      model: 'synthetic', costContext: { estimatedCost: amount, costUnit: 'USD', costMetadata: { source: 'provider_reported' } },
    } });
    span.end();
    await flush();
    assert.equal(spans.length, 1);
    assert.equal(spans[0].attrs['gen_ai.cost.total'], undefined);
    assert.equal(spans[0].attrs['mastra.cost.amount'], undefined);
  });
}
for (const [source, unit] of [['token_price_estimate', 'USD'], [undefined, 'USD'], ['provider_reported', 'EUR']]) {
  test(`native ${source} / ${unit} cost is retained without inventing provider USD spend`, async () => {
    const span = setup().startSpan({ type: 'model_generation', name: 'diagnostic-cost', attributes: {
      model: 'synthetic', costContext: { estimatedCost: 0.25, costUnit: unit, costMetadata: { source } },
    } });
    span.end();
    await flush();
    assert.equal(spans.length, 1);
    assert.equal(Number(spans[0].attrs['mastra.cost.amount']), 0.25);
    assert.equal(spans[0].attrs['mastra.cost.unit'], unit);
    assert.equal(spans[0].attrs['mastra.cost.source'], source);
    assert.equal(spans[0].attrs['gen_ai.cost.total'], undefined);
  });
}
