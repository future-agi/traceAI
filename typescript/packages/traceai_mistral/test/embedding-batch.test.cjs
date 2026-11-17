const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { once } = require('node:events');
const { context, trace, SpanStatusCode } = require('@opentelemetry/api');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { MistralInstrumentation } = require('../dist/src');

const manager = new AsyncLocalStorageContextManager().enable();
context.setGlobalContextManager(manager);
after(() => { context.disable(); manager.disable(); });

for (const sdk of ['mistral-current', 'mistral-floor']) {
  for (const size of [1, 140]) {
    for (const privateContent of [false, true]) {
      test(`${sdk}: embedding batch=${size} retains response metadata (private=${privateContent})`, async t => {
        const exporter = new InMemorySpanExporter();
        // Deliberately retain OTel's default 128-attribute limit.
        const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
        const instrumentation = new MistralInstrumentation({ traceConfig: {
          hideInputs: privateContent, hideOutputs: privateContent, hideEmbeddingVectors: privateContent,
        } });
        instrumentation.setTracerProvider(provider);
        const calls = [];
        const data = Array.from({ length: size }, (_, index) => ({ object: 'embedding', index, embedding: [912345.6, index] }));
        const server = http.createServer(async (req, res) => {
          let raw = '';
          for await (const chunk of req) raw += chunk;
          calls.push(JSON.parse(raw));
          res.writeHead(200, { 'content-type': 'application/json' });
          res.end(JSON.stringify({ id: 'embedding-id', object: 'list', model: 'resolved-embedding-model', data,
            usage: { prompt_tokens: size, total_tokens: size } }));
        });
        t.after(async () => {
          instrumentation.disable();
          await provider.shutdown();
          server.closeAllConnections();
          await new Promise(resolve => server.close(resolve));
        });
        server.listen(0, '127.0.0.1');
        await once(server, 'listening');
        // The 0.x SDK installs global fetch polyfills when first imported.
        const fetchGlobals = Object.fromEntries(['fetch', 'Request', 'Response', 'Headers'].map(key => [key, globalThis[key]]));
        t.after(() => Object.assign(globalThis, fetchGlobals));
        const mod = await import(sdk);
        const legacy = typeof mod.Mistral !== 'function';
        const url = `http://127.0.0.1:${server.address().port}`;
        const client = legacy ? new mod.default('synthetic-key', url) : new mod.Mistral({ apiKey: 'synthetic-key', serverURL: url });
        instrumentation.instrumentClient(client);
        const parent = provider.getTracer('test').startSpan('parent');
        const input = Array.from({ length: size }, (_, index) => `synthetic-input-${index}`);
        try {
          const result = await context.with(trace.setSpan(context.active(), parent), () => legacy
            ? client.embeddings({ model: 'requested-embedding-alias', input })
            : client.embeddings.create({ model: 'requested-embedding-alias', inputs: input }));
          assert.deepEqual(result.data, data);
          assert.equal(calls.length, 1);
          assert.deepEqual(calls[0].input ?? calls[0].inputs, input);
          assert.equal(calls[0].model, 'requested-embedding-alias');
          assert.equal(exporter.getFinishedSpans().length, 1);
          const span = exporter.getFinishedSpans()[0];
          const attrs = span.attributes;
          assert.equal(span.parentSpanContext.spanId, parent.spanContext().spanId);
          assert.equal(span.status.code, SpanStatusCode.OK);
          const expected = {
            'fi.span.kind': 'EMBEDDING',
            'gen_ai.provider.name': 'mistralai',
            'gen_ai.request.model': 'requested-embedding-alias',
            // PENDING I1-STRICTER: hideEmbeddingVectors also hides embedding.model_name.
            'embedding.model_name': privateContent ? '__REDACTED__' : 'requested-embedding-alias',
            'gen_ai.response.model': 'resolved-embedding-model',
            'gen_ai.response.id': 'embedding-id',
            'gen_ai.usage.input_tokens': size,
            'gen_ai.usage.total_tokens': size,
          };
          assert.deepEqual(Object.fromEntries(Object.keys(expected).map(key => [key, attrs[key]])), expected);
          if (privateContent) assert.doesNotMatch(JSON.stringify(attrs), /synthetic-input-|912345\.6/);
          else {
            assert.equal(attrs['embedding.embeddings.0.embedding.text'], input[0]);
            assert.deepEqual(attrs['embedding.embeddings.0.embedding.vector'], data[0].embedding);
          }
          assert.doesNotMatch(JSON.stringify(attrs), /synthetic-key/);
        } finally {
          parent.end();
        }
      });
    }
  }
}
