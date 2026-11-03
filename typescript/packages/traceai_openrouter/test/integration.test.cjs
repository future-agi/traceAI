const { test } = require('node:test');
const assert = require('node:assert/strict');
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { createOpenRouterTracingClient } = require('../dist/src/index.js');

for (const moduleName of ['@openrouter/sdk-floor', '@openrouter/sdk']) {
  test(`${moduleName}: official SDK inference routes, error, stream and cancellation`, async () => {
    const { OpenRouter } = await import(moduleName);
    assert.equal(typeof (await import('../dist/src/index.js')).createOpenRouterTracingClient, 'function');
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const paths = [];
    const callModelHeaders = [];
    let next = { id: 'synthetic-id', object: 'chat.completion', created: 1, system_fingerprint: 'synthetic', model: 'actual/model', usage: { prompt_tokens: 3, completion_tokens: 4, total_tokens: 7 }, choices: [{ index: 0, message: { role: 'assistant', content: 'synthetic' }, finish_reason: 'stop' }] };
    const fetcher = async (input) => {
      const req = input instanceof Request ? input : new Request(input);
      const path = new URL(req.url).pathname;
      paths.push(path);
      callModelHeaders.push(req.headers.get("x-openrouter-callmodel"));
      const body = JSON.stringify(next);
      if (next === 'error') return new Response('{}', { status: 400, headers: { 'content-type': 'application/json' } });
      if (next === 'stream') return new Response('data: {"id":"synthetic-id","object":"chat.completion.chunk","created":1,"model":"stream/model","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":6,"total_tokens":11}}\n\ndata: [DONE]\n\n', { headers: { 'content-type': 'text/event-stream' } });
      if (next === 'cancel') {
        const encoder = new TextEncoder();
        return new Response(new ReadableStream({ start(controller) { controller.enqueue(encoder.encode('data: {"model":"early"}\n\n')); } }), { headers: { 'content-type': 'text/event-stream' } });
      }
      return new Response(body, { headers: { 'content-type': 'application/json', 'content-length': String(Buffer.byteLength(body)) } });
    };
    const client = new OpenRouter({ apiKey: 'synthetic-only', httpClient: createOpenRouterTracingClient({ fetcher, tracer: provider.getTracer('test') }), retryConfig: { strategy: 'none' } });
    try {
      await client.chat.send({ chatRequest: { model: 'requested/model', messages: [{ role: 'user', content: 'synthetic question' }] } });
      assert.equal(paths.at(-1), '/api/v1/chat/completions');
      const span = exporter.getFinishedSpans().at(-1);
      assert.equal(span.attributes['gen_ai.provider.name'], 'openrouter');
      assert.equal(span.attributes['gen_ai.usage.input_tokens'], 3);
      assert.equal(span.attributes['gen_ai.usage.output_tokens'], 4);
      assert.equal(span.attributes['gen_ai.response.model'], 'actual/model');
      assert.equal(span.attributes['gen_ai.request.model'], 'requested/model');
      assert.equal(JSON.stringify(span.attributes).includes('synthetic question'), false);
      next = 'stream';
      const stream = await client.chat.send({ chatRequest: { model: 'requested/model', messages: [{ role: 'user', content: 'synthetic' }], stream: true } });
      for await (const _event of stream) { /* consume */ }
      assert.equal(exporter.getFinishedSpans().at(-1).attributes['gen_ai.usage.output_tokens'], 6);
      next = 'error';
      await assert.rejects(() => client.chat.send({ chatRequest: { model: 'requested/model', messages: [{ role: 'user', content: 'synthetic' }] } }));
      assert.equal(exporter.getFinishedSpans().at(-1).status.code, 2);
      assert.equal(JSON.stringify(exporter.getFinishedSpans().map((span) => span.attributes)).includes('synthetic question'), false);
      // Exercise every official SDK namespace, not just a manually constructed Request.
      for (const [invoke, endpoint, operation] of [
        [() => client.embeddings.generate({ requestBody: { model: 'synthetic', input: 'synthetic' } }), '/api/v1/embeddings', 'embeddings'],
        [() => client.rerank.rerank({ requestBody: { model: 'synthetic', query: 'synthetic', documents: ['synthetic'] } }), '/api/v1/rerank', 'rerank'],
        [() => client.responses.send({ responsesRequest: { model: 'synthetic', input: 'synthetic' } }), '/api/v1/responses', 'responses'],
        [() => client.beta.responses.send({ responsesRequest: { model: 'synthetic', input: 'synthetic' } }), '/api/v1/responses', 'responses'],
      ]) {
        await invoke().catch(() => {}); // Fake chat payload is invalid for other SDK response validators.
        assert.equal(paths.at(-1), endpoint);
        assert.equal(exporter.getFinishedSpans().at(-1).attributes['gen_ai.operation.name'], operation);
      }
      // callModel delegates to Responses: keep one model span per network round.
      next = 'error';
      await client.callModel({ model: 'synthetic/model', input: 'synthetic' }).getResponse().catch(() => {});
      assert.equal(paths.at(-1), '/api/v1/responses');
      assert.equal(callModelHeaders.at(-1), 'true');
      assert.equal(exporter.getFinishedSpans().at(-1).attributes['gen_ai.operation.name'], 'callModel');
      next = 'stream';
      const beforeUnrelated = exporter.getFinishedSpans().length;
      await createOpenRouterTracingClient({ fetcher, tracer: provider.getTracer('test') }).request(new Request('https://openrouter.ai/api/v1/models', { method: 'GET' }));
      assert.equal(exporter.getFinishedSpans().length, beforeUnrelated);
      next = 'cancel';
      const transport = createOpenRouterTracingClient({ fetcher, tracer: provider.getTracer('test') });
      const response = await transport.request(new Request('https://openrouter.ai/api/v1/responses', { method: 'POST' }));
      const reader = response.body.getReader();
      await reader.read();
      await reader.cancel();
      assert.equal(exporter.getFinishedSpans().at(-1).attributes['gen_ai.operation.name'], 'responses');
      assert.equal(paths.at(-1), '/api/v1/responses');
      assert.equal(exporter.getFinishedSpans().at(-1).attributes['gen_ai.response.model'], 'early');
    } finally { await provider.shutdown(); }
  });
}
