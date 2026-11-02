const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { once } = require('node:events');
const { createRequire } = require('node:module');
const path = require('node:path');
const local = createRequire(process.env.VERTEX_INSTRUMENTATION ? path.resolve(process.env.VERTEX_INSTRUMENTATION, 'package.json') : path.resolve(__dirname, '../package.json'));
const consumer = process.env.VERTEX_CONSUMER ? createRequire(path.resolve(process.env.VERTEX_CONSUMER, 'package.json')) : local;

test('automatic hook exports actual OTLP JSON with FI auth and one span per call', async () => {
  const requests = [];
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => { requests.push({ url: req.url, headers: req.headers, body: JSON.parse(body) }); res.setHeader('content-type', 'application/json'); res.end('{}'); });
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const originalFetch = global.fetch;
  let instrumentation, provider;
  const { context } = local('@opentelemetry/api');
  try {
    const { register, ProjectType } = local('@traceai/fi-core');
    const core = createRequire(local.resolve('@traceai/fi-core'));
    const { AsyncLocalStorageContextManager } = core('@opentelemetry/context-async-hooks');
    context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
    provider = register({ projectName: 'vertexai-compat', projectType: ProjectType.OBSERVE, setGlobalTracerProvider: true, batch: false,
      endpoint: `http://127.0.0.1:${server.address().port}`, headers: { 'x-api-key': 'synthetic-fi-key', 'x-secret-key': 'synthetic-fi-secret' } });
    const { VertexAIInstrumentation, isPatched } = local('@traceai/fi-instrumentation-vertexai');
    instrumentation = new VertexAIInstrumentation();
    instrumentation.setTracerProvider(provider);
    const sdk = consumer('@google-cloud/vertexai');
    assert.equal(isPatched(), true, 'CommonJS require must activate the automatic hook');
    // An ESM-style namespace alias must not duplicate automatic CJS hooks.
    instrumentation.manuallyInstrument(Object.freeze({ ...sdk }));
    let calls = 0;
    global.fetch = async (url, options) => {
      calls++;
      assert.match(String(url), /aiplatform.googleapis.com/);
      assert.doesNotMatch(JSON.stringify(options.headers), /synthetic-fi-key|synthetic-fi-secret/);
      return new Response(JSON.stringify({ candidates: [{ content: { role: 'model', parts: [{ text: 'four' }] }, finishReason: 'STOP' }], usageMetadata: { promptTokenCount: 4, candidatesTokenCount: 1, totalTokenCount: 5 } }));
    };
    const vertex = new sdk.VertexAI({ project: 'synthetic-project', location: 'us-central1', googleAuthOptions: { authClient: { getAccessToken: async () => ({ token: 'synthetic-google-token' }) } } });
    await vertex.getGenerativeModel({ model: 'gemini-test' }).generateContent('What is two plus two?');
    await provider.forceFlush();
    assert.equal(calls, 1);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, '/tracer/v1/traces');
    assert.equal(requests[0].headers['x-api-key'], 'synthetic-fi-key');
    assert.equal(requests[0].headers['x-secret-key'], 'synthetic-fi-secret');
    const spans = requests.flatMap(request => request.body.resourceSpans).flatMap(resource => resource.scopeSpans).flatMap(scope => scope.spans);
    assert.equal(spans.length, 1);
    const attrs = Object.fromEntries(spans[0].attributes.map(({ key, value }) => [key, value.stringValue ?? Number(value.intValue)]));
    assert.equal(attrs['gen_ai.request.model'], 'gemini-test');
    assert.equal(attrs['gen_ai.usage.input_tokens'], 4);
    assert.match(attrs['gen_ai.input.messages'], /two plus two/);
    assert.match(attrs['gen_ai.output.messages'], /four/);
    assert.doesNotMatch(JSON.stringify(requests[0].body), /synthetic-google-token|synthetic-fi-key|synthetic-fi-secret/);
    instrumentation.disable();
    await vertex.getGenerativeModel({ model: 'gemini-test' }).generateContent('untraced');
    await provider.forceFlush();
    assert.equal(requests.length, 1);
  } finally {
    instrumentation?.disable();
    await provider?.shutdown();
    context.disable();
    global.fetch = originalFetch;
    server.close();
    await once(server, 'close');
  }
});
