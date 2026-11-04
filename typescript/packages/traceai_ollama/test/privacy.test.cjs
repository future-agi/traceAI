// Inspect the actual OTLP/HTTP payload, not mocked FITracer inputs.
const assert = require('node:assert/strict');
const { test, before, after } = require('node:test');
const { createRequire } = require('node:module');
const { createServer } = require('node:http');
const { once } = require('node:events');
const coreRequire = createRequire(require.resolve('@traceai/fi-core'));
const { BasicTracerProvider, SimpleSpanProcessor } = coreRequire('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = coreRequire('@opentelemetry/exporter-trace-otlp-http');
const { OllamaInstrumentation } = require('../dist/cjs');
const { SemanticConventions: S } = require('@traceai/fi-semantic-conventions');
const sdk = require(process.env.OLLAMA_SDK_PATH || 'ollama');
const text = ['synthetic-prompt', 'synthetic-system', 'synthetic-template', 'synthetic-suffix'];
const image = Buffer.from('synthetic-private-image').toString('base64');
let server, host, bodies, requests;

before(async () => {
  server = createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    if (req.url === '/v1/traces') {
      bodies.push(JSON.parse(body));
      res.writeHead(200, { 'content-type': 'application/json' }); res.end('{}');
    } else {
      assert.equal(req.url, '/api/generate');
      const request = JSON.parse(body);
      requests.push(request);
      const response = { model: 'offline-model', response: 'safe-output', done: true, prompt_eval_count: 3, eval_count: 2 };
      res.writeHead(200, { 'content-type': request.stream ? 'application/x-ndjson' : 'application/json' });
      res.end(JSON.stringify(response) + (request.stream ? '\n' : ''));
    }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  host = `http://127.0.0.1:${server.address().port}`;
});
after(async () => {
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
});

const settings = [
  ['visible', {}, undefined],
  ['hideInputs', { hideInputs: true }, undefined],
  ['hideInputText', { hideInputText: true }, undefined],
  ['hideInputMessages', { hideInputMessages: true }, undefined],
  ['hideInputImages', { hideInputImages: true }, undefined],
  ['FI_HIDE_INPUTS', {}, 'FI_HIDE_INPUTS'],
  ['FI_HIDE_INPUT_TEXT', {}, 'FI_HIDE_INPUT_TEXT'],
  ['FI_HIDE_INPUT_MESSAGES', {}, 'FI_HIDE_INPUT_MESSAGES'],
  ['FI_HIDE_INPUT_IMAGES', {}, 'FI_HIDE_INPUT_IMAGES'],
];
for (const stream of [false, true]) {
  for (const [name, traceConfig, env] of settings) {
    test(`generate ${stream ? 'stream' : 'response'} privacy: ${name}`, async () => {
      bodies = []; requests = [];
      const oldEnv = env ? process.env[env] : undefined;
      if (env) process.env[env] = 'true';
      const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(
        new OTLPTraceExporter({ url: `${host}/v1/traces`, headers: {} }),
      )] });
      const instrumentation = new OllamaInstrumentation({ instrumentationConfig: { enabled: false }, traceConfig });
      instrumentation.setTracerProvider(provider);
      // Class-only setup isolates privacy from the singleton/constructor regression.
      const module = { Ollama: sdk.Ollama };
      instrumentation.manuallyInstrument(module);
      try {
        const request = { model: 'offline-model', prompt: text[0], system: text[1], template: text[2], suffix: text[3],
          images: [image], stream, raw: true, keep_alive: '5m', context: [100, 200],
          format: { type: 'object', description: 'synthetic-schema-content' },
          options: { temperature: 0.2, num_predict: 12, stop: ['synthetic-stop-content'] },
          future_content_field: 'synthetic-unknown-content' };
        const result = await new sdk.Ollama({ host }).generate(request);
        if (stream) { for await (const chunk of result) assert.equal(chunk.response, 'safe-output'); }
        else assert.equal(result.response, 'safe-output');
        await provider.forceFlush();
        assert.equal(requests.length, 1);
        // Instrumentation must never sanitize the request sent to the SDK/server.
        for (const field of ['prompt', 'system', 'template', 'suffix', 'images']) {
          assert.deepEqual(requests[0][field], request[field]);
        }
        const spans = bodies.flatMap(b => b.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
        assert.equal(spans.length, 1, 'exactly one span exported');
        const attrs = Object.fromEntries(spans[0].attributes.map(a => [a.key, Object.values(a.value)[0]]));
        const parameters = JSON.parse(attrs[S.LLM_INVOCATION_PARAMETERS]);
        assert.equal(parameters.options.temperature, 0.2);
        assert.equal(parameters.options.num_predict, 12);
        assert.equal(parameters.stream, stream);
        assert.equal(parameters.raw, true);
        assert.equal(parameters.keep_alive, '5m');
        const serialized = JSON.stringify(spans);
        if (name === 'visible') {
          for (const secret of [...text, image]) assert.ok(serialized.includes(secret), 'input remains visible by default');
        } else if (name.toLowerCase().includes('images')) {
          assert.ok(!serialized.includes(image), `${name}: image leaked onto the OTLP wire`);
        } else {
          for (const secret of text) assert.ok(!serialized.includes(secret), `${name}: input text leaked onto the OTLP wire`);
          if (name !== 'hideInputText' && name !== 'FI_HIDE_INPUT_TEXT') assert.ok(!serialized.includes(image));
        }
        // No prompt-derived content is allowed in invocation parameters, even without hiding.
        for (const secret of [...text, image, 'synthetic-schema-content', 'synthetic-stop-content', 'synthetic-unknown-content']) {
          assert.ok(!JSON.stringify(parameters).includes(secret), `${name}: invocation parameters contain ${secret}`);
        }
        assert.equal(parameters.context, undefined, 'prior conversation tokens are content, not tuning parameters');
      } finally {
        instrumentation.unpatch(module);
        instrumentation.disable();
        await provider.shutdown();
        if (env) { if (oldEnv === undefined) delete process.env[env]; else process.env[env] = oldEnv; }
      }
    });
  }
}
