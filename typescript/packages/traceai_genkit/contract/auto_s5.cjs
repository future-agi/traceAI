// Zero-code S5 regression: real register bootstrap, Genkit and loopback fetch.
// The exporter factory is replaced ONLY by this harness to record the already
// sanitized FI branch. Application code never calls init/enable/registerSecret.
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const Module = require('node:module');
const mode = process.argv[2];
const modes = ['signed-url', 'context-url', 'fi-wrapper', 'late-context', 'background', 'failed-context', 'transport', 'json-string', 'numeric', 'catalog-header', 'catalog-field', 'catalog-media'];
assert.ok(modes.includes(mode), 'Pass one of: ' + modes.join(', '));
const transformed = ['signed-url', 'context-url', 'json-string', 'numeric'].includes(mode);
// These cases deliberately use the shared golden bundle, not Genkit-specific
// recognition rules or copied expectations. A failure belongs to core S6 adoption.
const fixtureName = {
  'catalog-header': 'pattern-token-headers',
  'catalog-field': 'pattern-password-response',
  'catalog-media': 'magic-png-standard-response_body',
}[mode];
const fixture = fixtureName && require('../../../../spec/auto-instrumentation/privacy-fixtures.json').cases.find(item => item.name === fixtureName);
if (fixtureName) assert.ok(fixture, 'Missing shared privacy fixture: ' + fixtureName);
const hide = process.env.VERIFY_HIDE === '1';
const hideInputs = !['context-url', 'transport', 'background', 'catalog-header', 'catalog-field', 'catalog-media'].includes(mode);
process.env.GENKIT_ENV = 'prod';
delete process.env.GENKIT_TELEMETRY_SERVER;
process.env.FI_API_KEY = 'synthetic-fi-api-s5';
process.env.FI_SECRET_KEY = 'synthetic-fi-secret-s5';
process.env.FI_PROJECT_NAME = 'genkit-zero-code-s5';
process.env.FI_INSTRUMENTATIONS = 'genkit,wire';
process.env.FI_DISABLED_INSTRUMENTATIONS = '';
process.env.FI_WIRE_CAPTURE = 'true';
process.env.FI_CONFIG_FILE = path.join(__dirname, 'nonexistent-s5-config.toml');
process.env.OTEL_SDK_DISABLED = 'false';
for (const key of ['INPUTS', 'OUTPUTS', 'INPUT_MESSAGES', 'OUTPUT_MESSAGES', 'INPUT_TEXT', 'OUTPUT_TEXT', 'LLM_INVOCATION_PARAMETERS', 'INPUT_IMAGES', 'ERROR_DETAILS']) {
  process.env['FI_HIDE_' + key] = String(key === 'INPUTS' && hideInputs || hide && ['OUTPUTS', 'ERROR_DETAILS'].includes(key));
}
const { context, createContextKey, trace } = require('@opentelemetry/api');
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { FITracer } = require('@traceai/fi-core');
const nativeExporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(nativeExporter)] });
assert.equal(trace.setGlobalTracerProvider(provider), true);
const spans = [];
const otel = require(path.join(path.dirname(require.resolve('@traceai/fi-core')), 'otel.js'));
const factory = otel.createSpanExporter;
otel.createSpanExporter = () => ({
  export(batch, done) { spans.push(...batch); done({ code: 0 }); },
  async forceFlush() {}, async shutdown() {},
});
// Exercise either package build or the exact source under Jest/ts-node review.
const resolve = Module._resolveFilename;
if (process.env.VERIFY_SOURCE === '1') Module._resolveFilename = function(name, ...args) {
  return name === '@traceai/genkit' ? path.join(__dirname, '../src/index.ts') : resolve.call(this, name, ...args);
};
let endpoint;
const requests = [];
const server = http.createServer((req, res) => {
  const header = mode === 'catalog-header' ? req.headers['x-vault-token'] : req.headers.authorization;
  requests.push(header);
  const token = (header || '').replace(/^Bearer /, '');
  req.resume();
  res.setHeader('content-type', 'application/json');
  res.end(JSON.stringify({ echo: 'echo ' + token }));
});
const token = mode === 'numeric' ? '938417628354'
  : mode === 'catalog-header' ? fixture.input.value['x-vault-token']
  : mode === 'catalog-field' ? fixture.input.value.result.database_password
  : mode === 'catalog-media' ? fixture.input.value
  : 's5-' + mode + '-credential-123456789';
const secondary = 's5-secondary-credential-987654321';
let agent;
let result;
let skip;
async function echo(credential) {
  const response = await fetch(endpoint + '/credential-echo', { method: 'POST', headers: {
    ...(mode === 'catalog-header' ? fixture.input.value : { authorization: 'Bearer ' + credential }),
    'content-type': 'application/json',
  }, body: JSON.stringify({ ordinary: 'visible request' }) });
  assert.equal(response.status, 200);
  return (await response.json()).echo;
}
const echoChecks = [];
function markEcho(span, credentials) {
  const text = credentials.join(' and ');
  span.setAttribute('output.value', 'echo ' + text);
  // An ordinary key proves value-based registry scrubbing, not name-based hiding.
  span.setAttribute('custom.values', credentials.map(value => 'echo ' + value));
  span.setStatus({ code: 2, message: 'status ' + text });
  span.addEvent('event ' + text, { 'exception.message': 'event ' + text });
  echoChecks.push({ span, credentials });
}
function appEcho(credential, name = 'application child') {
  const span = provider.getTracer('application').startSpan(name);
  markEcho(span, [credential]);
  span.end();
  return span;
}
function assertEchoSnapshots() {
  for (const { span, credentials } of echoChecks) {
    const copy = spans.find(item => item.spanContext().spanId === span.spanContext().spanId);
    assert.ok(copy, 'Every application echo exports exactly once');
    const text = credentials.map(() => '<redacted>').join(' and ');
    assert.equal(copy.attributes['output.value'], 'echo ' + text);
    assert.deepEqual(copy.attributes['custom.values'], credentials.map(() => 'echo <redacted>'));
    assert.equal(copy.status.message, 'status ' + text);
    assert.equal(copy.events[0].name, 'event ' + text);
    assert.equal(copy.events[0].attributes['exception.message'], 'event ' + text);
    // The other exporter must still receive every original value and surface.
    const original = nativeExporter.getFinishedSpans().find(item => item.spanContext().spanId === span.spanContext().spanId);
    assert.equal(original.attributes['output.value'], 'echo ' + credentials.join(' and '));
    assert.deepEqual(original.attributes['custom.values'], credentials.map(value => 'echo ' + value));
    assert.equal(original.status.message, 'status ' + credentials.join(' and '));
    assert.equal(original.events[0].name, 'event ' + credentials.join(' and '));
    assert.equal(original.events[0].attributes['exception.message'], 'event ' + credentials.join(' and '));
  }
}
(async () => {
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    endpoint = 'http://127.0.0.1:' + server.address().port;
    process.env.FI_BASE_URL = endpoint;
    require('../../traceai_auto/src/register.ts'); // Actual zero-code entry point.
    agent = require('../../traceai_auto/src/index.ts');
    assert.equal(agent.report().active, true);
    assert.equal(agent.report().integrations.find(item => item.name === 'genkit').status, 'instrumented');
    const { genkit, z } = require('genkit');
    const { mockModel } = require('genkit/testing');
    const ai = genkit({});
    if (mode === 'transport' || mode === 'catalog-header') {
      const { shouldSkip } = require('../../traceai_auto/src/wire/fetch.ts');
      const model = mockModel(ai, { name: 's5/transport', respond: async () =>
        provider.getTracer('native-sdk').startActiveSpan('native transport intermediate', async intermediate => {
          try {
            skip = shouldSkip(context.active());
            assert.equal(skip, true, 'Coverage survives an intermediate native span');
            const text = await echo(token);
            const child = appEcho(token);
            assert.ok(spans.some(item => item.spanContext().spanId === child.spanContext().spanId), 'Covered child exports before its model ends');
            assert.equal(spans.some(item => item.name === 's5/transport'), false, 'The covering model is still open');
            return { text };
          } finally { intermediate.end(); }
        })
      });
      const response = await ai.generate({ model, prompt: 'ordinary prompt' });
      assert.equal(typeof response.toJSON, 'function');
      response.assertValid();
      result = response.text;
      assert.equal(result, 'echo ' + token);
    } else if (mode === 'catalog-field' || mode === 'catalog-media') {
      assert.equal(fixture.input.site, 'response_body');
      const value = fixture.input.path ? { [fixture.input.path]: fixture.input.value } : fixture.input.value;
      const flow = ai.defineFlow({ name: 'catalogFlow', inputSchema: z.string(), outputSchema: z.any() }, async () => value);
      result = await flow('ordinary input');
      assert.strictEqual(result, value, 'Instrumentation preserves the original application result');
    } else if (mode === 'late-context') {
      let child;
      const flow = ai.defineFlow({ name: 'lateContextFlow', inputSchema: z.object({ apiKey: z.string() }), outputSchema: z.string() }, async () => {
        await echo(token);
        child = provider.getTracer('application').startSpan('late child');
        await echo(secondary);
        markEcho(child, [token, secondary]);
        return 'echo ' + token;
      });
      result = await flow({ apiKey: token });
      assert.equal(result, 'echo ' + token);
      assert.equal(child.ended, false);
      assert.ok(spans.some(item => item.name === 'lateContextFlow'), 'The root exports before its still-open child');
      child.end();
    } else if (mode === 'background') {
      let release;
      let background;
      const gate = new Promise(resolve => { release = resolve; });
      const tool = ai.defineTool({ name: 'backgroundTool', description: 'echo', inputSchema: z.object({ apiKey: z.string() }), outputSchema: z.string() }, async () => {
        const response = await echo(token);
        appEcho(token);
        assert.ok(spans.some(span => span.name === 'application child'));
        return response;
      });
      const flow = ai.defineFlow({ name: 'completedRoot', inputSchema: z.string(), outputSchema: z.string() }, async () => {
        background = (async () => { await gate; return tool({ apiKey: token }); })();
        return 'completed root';
      });
      assert.equal(await flow('ordinary input'), 'completed root');
      assert.ok(spans.some(span => span.name === 'completedRoot'));
      release();
      result = await background;
      assert.equal(result, 'echo ' + token);
    } else if (mode === 'fi-wrapper' || mode === 'failed-context') {
      const fi = new FITracer({ tracer: provider.getTracer('fi-application') });
      const flow = ai.defineFlow({ name: 'wrapperFlow', inputSchema: z.object({ apiKey: z.string() }), outputSchema: z.string() }, async () => {
        await echo(token);
        if (mode === 'failed-context') {
          // A real, parsed transport body exhausts the shared discovery budget.
          // No test-only registry mutation: the core observer must fail closed.
          const response = await fetch(endpoint + '/v1/chat/completions', { method: 'POST', headers: {
            authorization: 'Bearer ' + token, 'content-type': 'application/json',
          }, body: JSON.stringify({ model: 'mock', messages: [{ role: 'user', content: 'ordinary prompt' }], padding: Array(20010).fill('ordinary') }) });
          await response.text();
        }
        return fi.startActiveSpan('FI intermediate', async span => {
          try {
            const child = appEcho(token);
            assert.ok(spans.some(item => item.spanContext().spanId === child.spanContext().spanId)); // No root buffer.
            return 'echo ' + token;
          } finally { span.end(); }
        });
      });
      // The old null sentinel must not be overwritten into empty evidence; S5
      // ignores obsolete per-trace credentials and uses the observed registry.
      result = mode === 'failed-context'
        ? await context.with(context.active().setValue(createContextKey('traceai.auto.library.credentials'), null), () => flow({ apiKey: token }))
        : await flow({ apiKey: token });
      assert.equal(result, 'echo ' + token);
    } else {
      // No transport ever sees these secrets: application transformation is OTHER.
      const flow = ai.defineFlow({ name: 'transformedFlow', inputSchema: z.any(), outputSchema: z.string() }, async input => {
        if (mode === 'signed-url') return 'echo ' + new URL(input).searchParams.get('X-Amz-Signature');
        if (mode === 'json-string') return 'echo ' + JSON.parse(input).apiKey;
        if (mode === 'numeric') return 'echo ' + String(input.apiKey);
        return 'echo ' + token;
      });
      const url = 'https://storage.invalid/object?X-Amz-Signature=' + token;
      const input = mode === 'signed-url' ? url : mode === 'json-string' ? JSON.stringify({ apiKey: token }) : mode === 'numeric' ? { apiKey: Number(token) } : 'ordinary input';
      result = mode === 'context-url' ? await flow(input, { context: { url } }) : await flow(input);
      assert.equal(result, 'echo ' + token);
    }
    await agent.forceFlush();
    const native = nativeExporter.getFinishedSpans();
    assert.ok(spans.length > 0);
    assert.equal(spans.length, native.length);
    assert.equal(new Set(spans.map(span => span.spanContext().spanId)).size, spans.length);
    assert.equal(spans.filter(span => span.attributes['traceai.capture.layer'] === 'wire').length, 0);
    for (const copy of spans) {
      const original = native.find(span => span.spanContext().spanId === copy.spanContext().spanId);
      assert.ok(original);
      for (const key of ['traceId', 'spanId', 'traceFlags']) assert.equal(copy.spanContext()[key], original.spanContext()[key]);
      assert.deepEqual(copy.startTime, original.startTime);
      assert.deepEqual(copy.endTime, original.endTime);
      assert.deepEqual(copy.duration, original.duration);
    }
    assert.ok(JSON.stringify(native.map(span => span.attributes)).includes(token), 'Native exporters retain the original application data');
    if (!hideInputs && !hide) assert.ok(spans.some(span => String(span.attributes['input.value']).includes('ordinary')), 'Content capture remains enabled by default');
    const tokenExported = JSON.stringify(spans).includes(token);
    if (fixtureName) console.log(JSON.stringify({ mode, integration: 'zero-code agent', mapping: process.env.VERIFY_SOURCE === '1' ? 'source' : 'built', fixture: fixtureName, skip, tokenExported, exported: spans.length, native: native.length, parity: 'passed' }));
    if ((mode === 'catalog-field' || mode === 'catalog-media') && !hide) {
      const expected = fixture.input.path ? { [fixture.input.path]: fixture.expected } : fixture.expected;
      assert.deepEqual(JSON.parse(spans[0].attributes['output.value']), expected, 'Shared golden privacy fixture: ' + fixtureName);
    }
    if (transformed && !hide) assert.equal(tokenExported, true, 'Pin the documented application-transformation limitation');
    else {
      assert.equal(tokenExported, false, 'Transport credentials and structurally marked secrets/media must not reach FI export');
      assert.equal(JSON.stringify(spans).includes(secondary), false);
    }
    if (mode === 'failed-context') {
      assert.ok(spans.every(span => Object.keys(span.attributes).length === 0 && span.name === '<redacted>'), 'Actual transport discovery failure keeps only identity and timing');
    }
    if (!transformed && mode !== 'catalog-field' && mode !== 'catalog-media') {
      assert.ok(requests.includes(mode === 'catalog-header' ? token : 'Bearer ' + token), 'Loopback received the unchanged application credential');
      // No ancestry: a subsequent unrelated span still receives registry scrubbing.
      appEcho(token, 'unrelated later span');
      await agent.forceFlush();
      assert.equal(JSON.stringify(spans).includes(token), false);
    }
    assert.equal(spans.length, nativeExporter.getFinishedSpans().length);
    if (mode !== 'failed-context') assertEchoSnapshots();
    console.log(JSON.stringify({ mode, integration: 'zero-code agent', mapping: process.env.VERIFY_SOURCE === '1' ? 'source' : 'built', hide, status: 'passed', classification: transformed ? 'OTHER' : 'fixed', tokenExported, exported: spans.length, native: nativeExporter.getFinishedSpans().length, exactEchoSnapshots: mode === 'failed-context' ? 0 : echoChecks.length }));
  } finally {
    if (agent) await agent.shutdown();
    await provider.shutdown();
    otel.createSpanExporter = factory;
    Module._resolveFilename = resolve;
    await new Promise(resolve => server.close(resolve));
    server.closeAllConnections();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
