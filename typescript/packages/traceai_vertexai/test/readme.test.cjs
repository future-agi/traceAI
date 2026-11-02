const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const http = require('node:http');
const { once } = require('node:events');
const path = require('node:path');
const root = process.env.VERTEX_INSTRUMENTATION || path.resolve(__dirname, '..');
const local = createRequire(path.join(root, 'package.json'));
const consumer = process.env.VERTEX_CONSUMER ? createRequire(path.resolve(process.env.VERTEX_CONSUMER, 'package.json')) : local;

test('the exact README application runs with synthetic authentication and loopback OTLP', async () => {
  const code = readFileSync(path.join(root, 'README.md'), 'utf8').match(/```javascript\n([\s\S]+?)```/)[1];
  const sdk = consumer('@google-cloud/vertexai');
  const core = local('@traceai/fi-core');
  const coreRequire = createRequire(local.resolve('@traceai/fi-core'));
  const finished = [], errors = [], printed = [];
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      finished.push(...JSON.parse(body).resourceSpans.flatMap(resource => resource.scopeSpans).flatMap(scope => scope.spans));
      res.setHeader('content-type', 'application/json'); res.end('{}');
    });
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const originalFetch = global.fetch;
  let provider;
  try {
    global.fetch = async () => new Response(JSON.stringify({ candidates: [{ content: { role: 'model', parts: [{ text: 'Light scatters in the atmosphere.' }] } }] }));
    const fakeRequire = name => {
      if (name === '@google-cloud/vertexai') return { ...sdk, VertexAI: class extends sdk.VertexAI {
        constructor(options) { super({ ...options, googleAuthOptions: { authClient: { getAccessToken: async () => ({ token: 'synthetic-only' }) } } }); }
      } };
      if (name === '@traceai/fi-core') return { ...core, register(options) {
        provider = core.register({ ...options, endpoint: `http://127.0.0.1:${server.address().port}`, setGlobalTracerProvider: false, headers: { 'x-api-key': 'synthetic', 'x-secret-key': 'synthetic' } });
        return provider;
      } };
      if (name === '@opentelemetry/context-async-hooks') return coreRequire(name);
      return local(name);
    };
    await vm.runInNewContext(code, { require: fakeRequire, process: { env: { GOOGLE_CLOUD_PROJECT: 'synthetic-project' } }, console: { log: value => printed.push(value), error: error => errors.push(error) } });
    assert.deepEqual(errors, []);
    assert.equal(printed[0][0].text, 'Light scatters in the atmosphere.');
    assert.equal(finished.length, 1);
    assert.equal(finished[0].attributes.find(attribute => attribute.key === 'gen_ai.request.model').value.stringValue, 'gemini-2.5-flash');
  } finally {
    await provider?.shutdown();
    local('@opentelemetry/api').context.disable();
    global.fetch = originalFetch;
    server.close();
    await once(server, 'close');
  }
});
