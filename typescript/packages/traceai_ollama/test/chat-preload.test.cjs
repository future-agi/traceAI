// Omitted messages are valid Ollama model preload/unload requests.
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
let server, host, bodies, requests;

before(async () => {
  server = createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    if (req.url === '/v1/traces') {
      bodies.push(JSON.parse(body));
      res.writeHead(200, { 'content-type': 'application/json' }); res.end('{}');
    } else {
      assert.equal(req.url, '/api/chat');
      const request = JSON.parse(body);
      requests.push(request);
      const response = { model: 'offline-model', message: { role: 'assistant', content: '' },
        done: true, done_reason: request.keep_alive === 0 ? 'unload' : 'load' };
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

for (const stream of [false, true]) {
  for (const keep_alive of ['5m', 0]) {
    test(`chat without messages: ${keep_alive === 0 ? 'unload' : 'preload'}, stream=${stream}`, async () => {
      bodies = []; requests = [];
      const request = { model: 'offline-model', stream, keep_alive };
      const client = new sdk.Ollama({ host });
      async function invoke() {
        const result = await client.chat(request);
        if (!stream) return result;
        const chunks = [];
        for await (const chunk of result) chunks.push(chunk);
        return chunks;
      }
      const baseline = await invoke();
      assert.equal(requests.length, 1, 'uninstrumented real SDK accepts omitted messages');
      const baselineRequest = requests[0];
      const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(
        new OTLPTraceExporter({ url: `${host}/v1/traces`, headers: {} }),
      )] });
      const instrumentation = new OllamaInstrumentation({ instrumentationConfig: { enabled: false } });
      instrumentation.setTracerProvider(provider);
      const module = { Ollama: sdk.Ollama };
      instrumentation.manuallyInstrument(module);
      try {
        assert.deepEqual(await invoke(), baseline, 'instrumentation preserves the real SDK result');
        await provider.forceFlush();
        assert.deepEqual(requests, [baselineRequest, baselineRequest], 'instrumentation preserves the provider request');
        assert.ok(!Object.hasOwn(request, 'messages'), 'instrumentation must not add messages to the caller request');
        const spans = bodies.flatMap(b => b.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
        assert.equal(spans.length, 1);
        const attrs = Object.fromEntries(spans[0].attributes.map(a => [a.key, Object.values(a.value)[0]]));
        assert.equal(attrs[S.LLM_INPUT_MESSAGES], '[]');
        assert.equal(attrs[S.LLM_MODEL_NAME], 'offline-model');
        assert.equal(JSON.parse(attrs[S.LLM_INVOCATION_PARAMETERS]).keep_alive, keep_alive);
      } finally {
        instrumentation.unpatch(module);
        instrumentation.disable();
        await provider.shutdown();
      }
    });
  }
}
