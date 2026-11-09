// Actual two-step Mastra Agent + thrown createTool error + decoded OTLP.
// Both the provider and collector are synthetic loopback servers.
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const fs = require('node:fs');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const packagePath = process.env.MASTRA_TEST_PACKAGE || path.resolve(__dirname, '../dist/src/index.js');
const req = createRequire(process.env.MASTRA_TEST_DEPS
  ? path.join(process.env.MASTRA_TEST_DEPS, 'package.json') : packagePath);
const { Mastra } = req('@mastra/core');
const { Agent } = req('@mastra/core/agent');
const { createTool } = req('@mastra/core/tools');
const { SpanType } = req('@mastra/core/observability');
const { z } = req('zod');
const { createOpenAI } = req('@ai-sdk/openai');
const transformer = createRequire(req.resolve('@opentelemetry/exporter-trace-otlp-proto'));
const wire = transformer('@opentelemetry/otlp-transformer/build/src/generated/root.js')
  .opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
const marker = 'TOOL_EXCEPTION_ONLY_PRIVATE_DETAIL';
const flags = ['ERROR_DETAILS', 'INPUTS', 'OUTPUTS', 'INPUT_MESSAGES', 'OUTPUT_MESSAGES',
  'INPUT_TEXT', 'OUTPUT_TEXT', 'INPUT_IMAGES'].map(flag => `FI_HIDE_${flag}`);
const attrs = list => Object.fromEntries((list || []).map(item => [item.key, Object.values(item.value)[0]]));
let createFIObservability, spans, requests, toolCalls;
const collector = http.createServer((request, response) => {
  const chunks = [];
  request.on('data', chunk => chunks.push(chunk));
  request.on('end', () => {
    assert.equal(request.headers['x-api-key'], 'synthetic-fi');
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
    requests.push(JSON.parse(body));
    assert.equal(request.headers.authorization, 'Bearer synthetic-tool-provider');
    const first = requests.length === 1;
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ id: 'tool-result-id', created: 1700000000, model: 'gpt-4o-mini',
      object: 'chat.completion', usage: { prompt_tokens: 4, completion_tokens: 2, total_tokens: 6 },
      choices: [{ index: 0, message: first
        ? { role: 'assistant', content: null, tool_calls: ['lookup', 'safeLookup'].map(name => ({
          id: `${name}-call`, type: 'function', function: { name, arguments: '{"query":"SAFE_TOOL_INPUT"}' },
        })) }
        : { role: 'assistant', content: 'SAFE_FINAL_OUTPUT' }, finish_reason: first ? 'tool_calls' : 'stop' }] }));
  });
});
before(async () => {
  ({ createFIObservability } = await import(pathToFileURL(packagePath).href));
  for (const server of [collector, provider]) await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
});
after(async () => {
  for (const server of [collector, provider]) {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
});
const policies = [
  ['default', {}, false],
  // Shared HIDE_ERROR_DETAILS_IMPLIED_BY deliberately excludes image-only
  // hiding; retain that negative control as well as every text/output policy.
  ...flags.map(flag => [flag, { [flag]: 'true' }, flag !== 'FI_HIDE_INPUT_IMAGES']),
  ['explicit-false', { FI_HIDE_ERROR_DETAILS: 'false' }, false],
  ...['OUTPUTS', 'OUTPUT_MESSAGES', 'OUTPUT_TEXT'].map(flag => [
    `${flag}-explicit-false`, { [`FI_HIDE_${flag}`]: 'true', FI_HIDE_ERROR_DETAILS: 'false' }, false,
  ]),
];
for (const [label, env, hidden] of policies) test(`thrown tool error and successful tool history: ${label}`, async () => {
  spans = []; requests = []; toolCalls = 0;
  const originalEnv = Object.fromEntries(flags.map(flag => [flag, process.env[flag]]));
  for (const flag of flags) delete process.env[flag];
  Object.assign(process.env, env);
  const native = [];
  const obs = createFIObservability({ serviceName: 'tool-error-wire', apiKey: 'synthetic-fi',
    secretKey: 'synthetic-fi-secret', endpoint: `http://127.0.0.1:${collector.address().port}/tracer/v1/traces` });
  const exporter = obs.getDefaultInstance().getExporters()[0];
  const send = exporter.exportTracingEvent.bind(exporter);
  exporter.exportTracingEvent = event => {
    if (event.type === 'span_ended') native.push(event.exportedSpan);
    return send(event);
  };
  try {
    const options = { description: 'Synthetic lookup', inputSchema: z.object({ query: z.string() }) };
    const lookup = createTool({ ...options, id: 'lookup', execute: async () => { toolCalls++; throw new Error(marker); } });
    const safeLookup = createTool({ ...options, id: 'safeLookup', execute: async () => { toolCalls++; return 'SAFE_TOOL_RESULT'; } });
    const model = createOpenAI({ apiKey: 'synthetic-tool-provider',
      baseURL: `http://127.0.0.1:${provider.address().port}/v1` }).chat('gpt-4o-mini');
    const agent = new Agent({ id: 'tool-error', name: 'Tool error', instructions: 'Use the tools', model, tools: { lookup, safeLookup } });
    const mastra = new Mastra({ agents: { agent }, observability: obs });
    assert.equal((await mastra.getAgent('agent').generate('SAFE_USER_PROMPT', { maxSteps: 3 })).text, 'SAFE_FINAL_OUTPUT');
    await obs.shutdown();
    assert.equal(toolCalls, 2);
    assert.equal(requests.length, 2);
    assert.match(JSON.stringify(requests[1].messages), new RegExp(marker));
    assert.match(JSON.stringify(native), new RegExp(marker), 'native spans remain unchanged for other exporters');
    const tool = spans.filter(span => span.attrs['mastra.span.type'] === 'tool_call');
    assert.ok(tool.some(span => span.status?.code === 2 && span.name.includes('lookup')));
    assert.ok(tool.some(span => span.name.includes('safeLookup') && span.status?.code !== 2));
    assert.equal(tool.length, native.filter(span => span.type === 'tool_call').length);
    // Old native dispatchers can deliver duplicate ended events: the adapter
    // must preserve native multiplicity, not create additional observations.
    const counts = values => Object.fromEntries([...new Set(values)].sort().map(id => [id, values.filter(value => value === id).length]));
    assert.deepEqual(counts(spans.map(span => span.spanId)), counts(native.map(span => Buffer.from(span.id, 'hex').toString('base64'))));
    assert.doesNotMatch(JSON.stringify(spans), /synthetic-tool-provider|synthetic-fi-secret/);
    if (!Object.keys(env).some(key => key.startsWith('FI_HIDE_INPUT'))) {
      assert.match(JSON.stringify(spans), /SAFE_USER_PROMPT/);
      const history = spans.flatMap(span => Object.entries(span.attrs)
        .filter(([key]) => key === 'input.value' || /^mastra\..*\.input$/.test(key)).map(([, value]) => value));
      assert.match(JSON.stringify(history), /SAFE_TOOL_RESULT/, 'normal tool results remain in later input history');
      assert.match(JSON.stringify(history), /lookup-call/, 'tool-call identity is preserved');
    }
    if (process.env.MASTRA_EVIDENCE_DIR) {
      fs.mkdirSync(process.env.MASTRA_EVIDENCE_DIR, { recursive: true });
      fs.writeFileSync(path.join(process.env.MASTRA_EVIDENCE_DIR, `tool-${label}.json`), JSON.stringify({ env, native, spans }, null, 2));
    }
    if (hidden) assert.doesNotMatch(JSON.stringify(spans), new RegExp(marker), 'error must not escape via direct fields or later input history');
    else assert.match(JSON.stringify(spans), new RegExp(marker), 'non-redacted mode preserves error details');
  } finally {
    await obs.shutdown();
    for (const [flag, value] of Object.entries(originalEnv)) {
      if (value === undefined) delete process.env[flag]; else process.env[flag] = value;
    }
  }
});

test('root completion cannot discard provenance before a slow child conversion or immediate shutdown', async () => {
  spans = [];
  const originalEnv = Object.fromEntries(flags.map(flag => [flag, process.env[flag]]));
  for (const flag of flags) delete process.env[flag];
  process.env.FI_HIDE_ERROR_DETAILS = 'true';
  const obs = createFIObservability({ serviceName: 'tool-ordering', apiKey: 'synthetic-fi',
    secretKey: 'synthetic-fi-secret', endpoint: `http://127.0.0.1:${collector.address().port}/tracer/v1/traces` });
  const instance = obs.getDefaultInstance();
  const exporter = instance.getExporters()[0];
  // Fault-inject conversion scheduling, not Mastra's lifecycle or error shape.
  const convert = exporter.converter.convertSpan.bind(exporter.converter);
  exporter.converter.convertSpan = async span => {
    if (span.type === 'model_step') await new Promise(resolve => setTimeout(resolve, 25));
    return convert(span);
  };
  try {
    const root = instance.startSpan({ type: SpanType.AGENT_RUN, name: 'ordering-root' });
    const tool = instance.startSpan({ type: SpanType.TOOL_CALL, name: 'lookup', parent: root, entityName: 'lookup' });
    tool.error({ error: new Error(marker), endSpan: true });
    const input = { body: { messages: [{ role: 'tool', tool_call_id: 'failed-call', content: marker },
      { role: 'tool', tool_call_id: 'safe-call', content: 'SAFE_TOOL_RESULT' }] } };
    const step = instance.startSpan({ type: SpanType.MODEL_STEP, name: 'later-history', parent: root, input });
    step.end();
    root.end();
    await obs.shutdown();
    assert.equal(spans.length, 3);
    assert.doesNotMatch(JSON.stringify(spans), new RegExp(marker));
    assert.match(JSON.stringify(spans), /SAFE_TOOL_RESULT|failed-call/);
    assert.equal(input.body.messages[0].content, marker, 'native request is not mutated');
    assert.equal(exporter.toolErrors.matches({ traceId: root.traceId, id: root.id }, marker), false, 'shutdown clears provenance');
  } finally {
    await obs.shutdown();
    for (const [flag, value] of Object.entries(originalEnv)) {
      if (value === undefined) delete process.env[flag]; else process.env[flag] = value;
    }
  }
});
