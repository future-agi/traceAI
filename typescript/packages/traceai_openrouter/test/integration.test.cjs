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
      // Real HTTP chunked JSON responses often have no Content-Length.
      const unboundedHeaderClient = new OpenRouter({ apiKey: 'synthetic-only', retryConfig: { strategy: 'none' },
        httpClient: createOpenRouterTracingClient({ fetcher: async () => new Response(JSON.stringify(next), { headers: { 'content-type': 'application/json' } }), tracer: provider.getTracer('test') }) });
      const chunked = await unboundedHeaderClient.chat.send({ chatRequest: { model: 'requested/model', messages: [{ role: 'user', content: 'synthetic' }] } });
      assert.equal(chunked.usage.promptTokens, 3);
      const chunkedSpan = exporter.getFinishedSpans().at(-1);
      assert.equal(chunkedSpan.attributes['gen_ai.response.model'], 'actual/model');
      assert.equal(chunkedSpan.attributes['gen_ai.usage.input_tokens'], 3);
      assert.equal(chunkedSpan.attributes['gen_ai.usage.output_tokens'], 4);
      next = 'stream';
      const stream = await client.chat.send({ chatRequest: { model: 'requested/model', messages: [{ role: 'user', content: 'synthetic' }], stream: true } });
      for await (const _event of stream) { /* consume */ }
      assert.equal(exporter.getFinishedSpans().at(-1).attributes['gen_ai.usage.output_tokens'], 6);
      const malformedStreamClient = new OpenRouter({ apiKey: 'synthetic-only', retryConfig: { strategy: 'none' },
        httpClient: createOpenRouterTracingClient({ fetcher: async () => new Response('data: {bad json}\n\n', { headers: { 'content-type': 'text/event-stream' } }), tracer: provider.getTracer('test') }) });
      const malformed = await malformedStreamClient.chat.send({ chatRequest: { model: 'requested/model', messages: [{ role: 'user', content: 'synthetic' }], stream: true } });
      await assert.rejects(async () => { for await (const _event of malformed) { /* consume */ } });
      assert.equal(exporter.getFinishedSpans().at(-1).status.code, 2);
      const invalidSchemaClient = new OpenRouter({ apiKey: 'synthetic-only', retryConfig: { strategy: 'none' },
        httpClient: createOpenRouterTracingClient({ fetcher: async () => new Response('data: {"unexpected":"value"}\n\n', { headers: { 'content-type': 'text/event-stream' } }), tracer: provider.getTracer('test') }) });
      const invalidSchema = await invalidSchemaClient.chat.send({ chatRequest: { model: 'requested/model', messages: [{ role: 'user', content: 'synthetic' }], stream: true } });
      await assert.rejects(async () => { for await (const _event of invalidSchema) { /* consume */ } });
      assert.equal(exporter.getFinishedSpans().at(-1).status.code, 2);
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

for (const moduleName of ['@openrouter/sdk-floor', '@openrouter/sdk']) {
  test(`${moduleName}: 200 response validation, provider stream failures and semantic kinds`, async () => {
    const { OpenRouter } = await import(moduleName);
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    let reply = new Response('{}', { headers: { 'content-type': 'application/json' } });
    const client = new OpenRouter({ apiKey: 'synthetic-only', retryConfig: { strategy: 'none' },
      httpClient: createOpenRouterTracingClient({ fetcher: async () => reply.clone(), tracer: provider.getTracer('test') }) });
    try {
      for (const invoke of [
        () => client.chat.send({ chatRequest: { model: 'synthetic', messages: [{ role: 'user', content: 'synthetic' }] } }),
        () => client.embeddings.generate({ requestBody: { model: 'synthetic', input: 'synthetic' } }),
        () => client.rerank.rerank({ requestBody: { model: 'synthetic', query: 'synthetic', documents: ['synthetic'] } }),
        () => client.responses.send({ responsesRequest: { model: 'synthetic', input: 'synthetic' } }),
      ]) {
        await assert.rejects(invoke);
        assert.equal(exporter.getFinishedSpans().at(-1).status.code, 2);
      }
      for (const response of [
        new Response('{broken', { headers: { 'content-type': 'application/json' } }),
        new Response('{}', { status: 201, headers: { 'content-type': 'application/json' } }),
        new Response('{}', { headers: { 'content-type': 'text/plain' } }),
      ]) {
        reply = response;
        await assert.rejects(() => client.chat.send({ chatRequest: { model: 'synthetic', messages: [{ role: 'user', content: 'synthetic' }] } }));
        assert.equal(exporter.getFinishedSpans().at(-1).status.code, 2);
      }
      // The same validation must not falsely mark valid chat, embedding or rerank responses.
      for (const [invoke, payload, kind] of [
        [() => client.chat.send({ chatRequest: { model: 'synthetic', messages: [{ role: 'user', content: 'synthetic' }] } }),
          { id: 'synthetic-id', object: 'chat.completion', created: 1, system_fingerprint: 'synthetic', model: 'synthetic', choices: [{ index: 0, message: { role: 'assistant', content: 'synthetic' }, finish_reason: 'stop' }] }, 'LLM'],
        [() => client.embeddings.generate({ requestBody: { model: 'synthetic', input: 'synthetic' } }),
          { data: [], model: 'synthetic', object: 'list' }, 'EMBEDDING'],
        [() => client.rerank.rerank({ requestBody: { model: 'synthetic', query: 'synthetic', documents: ['synthetic'] } }),
          { model: 'synthetic', results: [] }, 'RERANKER'],
      ]) {
        reply = new Response(JSON.stringify(payload), { headers: { 'content-type': 'application/json' } });
        await invoke();
        assert.equal(exporter.getFinishedSpans().at(-1).status.code, 0);
        assert.equal(exporter.getFinishedSpans().at(-1).attributes['fi.span.kind'], kind);
      }
      for (const [data, type] of [
        [{ type: 'error', code: 'server_error', message: 'synthetic failure', param: null, sequence_number: 0 }, 'error'],
        [{ type: 'response.failed', sequence_number: 0, response: {} }, 'response.failed'],
      ]) {
        reply = new Response(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`, { headers: { 'content-type': 'text/event-stream' } });
        const stream = await client.responses.send({ responsesRequest: { model: 'synthetic', input: 'synthetic', stream: true } });
        if (type === 'error') {
          const events = [];
          for await (const event of stream) events.push(event);
          assert.equal(events[0].type, type);
        } else {
          try { for await (const _event of stream) { /* SDK may validate response.failed fields */ } } catch { /* failure event remains a failure */ }
        }
        assert.equal(exporter.getFinishedSpans().at(-1).status.code, 2);
      }
      // A clean byte-stream EOF with a response.failed event must itself mark an error,
      // independently of whether the SDK accepts the payload's nested response shape.
      const failureBody = 'event: response.failed\ndata: {"type":"response.failed","sequence_number":0,"response":{}}\n\n';
      const transport = createOpenRouterTracingClient({ fetcher: async () => new Response(failureBody, { headers: { 'content-type': 'text/event-stream' } }), tracer: provider.getTracer('test') });
      const failureResponse = await transport.request(new Request('https://openrouter.ai/api/v1/responses', { method: 'POST', body: '{}' }));
      await failureResponse.text();
      assert.equal(exporter.getFinishedSpans().at(-1).status.code, 2);
      for (const [path, kind] of [['embeddings', 'EMBEDDING'], ['rerank', 'RERANKER']]) {
        await createOpenRouterTracingClient({ fetcher: async () => new Response('{}', { headers: { 'content-type': 'application/json' } }), tracer: provider.getTracer('test') })
          .request(new Request(`https://openrouter.ai/api/v1/${path}`, { method: 'POST', body: '{}' }));
        assert.equal(exporter.getFinishedSpans().at(-1).attributes['fi.span.kind'], kind);
      }
    } finally { await provider.shutdown(); }
  });
}
