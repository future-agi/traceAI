// Real Ollama client + OTLP/HTTP exporter: inspect bytes received over loopback.
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
const secrets = ['synthetic-chat-message', 'synthetic-chat-schema', 'synthetic-chat-stop',
  'synthetic-tool-name', 'synthetic-tool-description', 'synthetic-tool-parameter', 'synthetic-chat-future'];
let server, host, bodies, requests;

before(async () => {
  server = createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    if (req.url === '/v1/traces') {
      bodies.push(body);
      res.writeHead(200, { 'content-type': 'application/json' }); res.end('{}');
    } else {
      assert.equal(req.url, '/api/chat');
      const request = JSON.parse(body);
      requests.push(request);
      const response = { model: 'offline-model', message: { role: 'assistant', content: 'safe-output' },
        done: true, prompt_eval_count: 3, eval_count: 2 };
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
  ['FI_HIDE_INPUTS', {}, 'FI_HIDE_INPUTS'],
  ['FI_HIDE_INPUT_TEXT', {}, 'FI_HIDE_INPUT_TEXT'],
  ['FI_HIDE_INPUT_MESSAGES', {}, 'FI_HIDE_INPUT_MESSAGES'],
  // Explicit code config must keep its precedence over the environment.
  ['visible-code-override', { hideInputs: false }, 'FI_HIDE_INPUTS'],
];
for (const stream of [false, true]) {
  for (const [name, traceConfig, env] of settings) {
    test(`chat ${stream ? 'stream' : 'response'} OTLP privacy: ${name}`, async () => {
      bodies = []; requests = [];
      const oldEnv = env ? process.env[env] : undefined;
      if (env) process.env[env] = 'true';
      const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(
        new OTLPTraceExporter({ url: `${host}/v1/traces`, headers: {} }),
      )] });
      const instrumentation = new OllamaInstrumentation({ instrumentationConfig: { enabled: false }, traceConfig });
      instrumentation.setTracerProvider(provider);
      const module = { Ollama: sdk.Ollama };
      instrumentation.manuallyInstrument(module);
      try {
        const request = { model: 'offline-model', messages: [{ role: 'user', content: secrets[0] }], stream,
          keep_alive: '5m', format: { type: 'object', description: secrets[1] },
          options: { temperature: 0.2, num_predict: 12, use_mmap: true, stop: [secrets[2]] },
          tools: [{ type: 'function', function: { name: secrets[3], description: secrets[4],
            parameters: { type: 'object', properties: { input: { type: 'string', description: secrets[5] } } } } }],
          future_content_field: secrets[6] };
        const before = JSON.stringify(request);
        const result = await new sdk.Ollama({ host }).chat(request);
        if (stream) { for await (const chunk of result) assert.equal(chunk.message.content, 'safe-output'); }
        else assert.equal(result.message.content, 'safe-output');
        await provider.forceFlush();
        assert.equal(JSON.stringify(request), before, 'caller request must not change');
        assert.deepEqual(requests, [request], 'provider receives the complete, unchanged request');
        const spans = bodies.flatMap(b => JSON.parse(b).resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
        assert.equal(spans.length, 1, 'exactly one span exported');
        const attrs = Object.fromEntries(spans[0].attributes.map(a => [a.key, Object.values(a.value)[0]]));
        assert.equal(attrs[S.LLM_MODEL_NAME], 'offline-model');
        assert.equal(Number(attrs[S.LLM_TOKEN_COUNT_PROMPT]), 3);
        assert.equal(Number(attrs[S.LLM_TOKEN_COUNT_COMPLETION]), 2);
        const wire = bodies.join('\n');
        if (name.startsWith('visible')) {
          for (const secret of secrets) {
            assert.ok(wire.includes(secret), `visible positive control missing ${secret}`);
            assert.ok(attrs[S.INPUT_VALUE].includes(secret), 'content retained in privacy-governed input');
          }
        } else {
          for (const secret of secrets) assert.ok(!wire.includes(secret), `${name}: OTLP wire leaked ${secret}`);
        }
        if (name.startsWith('visible')) {
          assert.deepEqual(JSON.parse(attrs[S.LLM_INVOCATION_PARAMETERS]), {
            model: 'offline-model', stream, keep_alive: '5m',
            options: { temperature: 0.2, num_predict: 12, use_mmap: true },
          }, 'invocation parameters contain only typed, content-free tuning values, even when input is visible');
        } else {
          // Shared core hides opaque request parameters: arbitrary fields can copy private inputs.
          assert.equal(attrs[S.LLM_INVOCATION_PARAMETERS], '__REDACTED__', 'input hiding redacts opaque invocation parameters');
        }
      } finally {
        instrumentation.disable();
        await provider.shutdown();
        if (env) { if (oldEnv === undefined) delete process.env[env]; else process.env[env] = oldEnv; }
      }
    });
  }
}
