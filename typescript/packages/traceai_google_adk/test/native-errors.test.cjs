'use strict';
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { trace, context, propagation } = require('@opentelemetry/api');
const { NodeTracerProvider } = require('@opentelemetry/sdk-trace-node');
const { SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { recordGoogleADKModelError } = require('../dist/src');

test('error callback enriches only the active span and never swallows or records error text', async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({spanProcessors: [new SimpleSpanProcessor(exporter)]});
  provider.register();
  try {
    const error = Object.assign(new Error('PRIVATE_ERROR'), {name: 'ApiError', status: 400});
    const params = {llmRequest: {model: 'gemini-test', contents: ['PRIVATE_INPUT']}, error};
    assert.equal(await recordGoogleADKModelError(params), undefined, 'no active span is harmless');
    const tracer = provider.getTracer('gcp.vertex.agent');
    await tracer.startActiveSpan('call_llm', async span => {
      assert.equal(await recordGoogleADKModelError(params), undefined, 'undefined lets ADK continue its error handling');
      span.end();
    });
    for (const status of ['400', -1, 999, NaN]) {
      await tracer.startActiveSpan('call_llm', async span => {
        await recordGoogleADKModelError({llmRequest: {}, error: Object.assign(new Error('PRIVATE_ERROR'), {status})});
        span.end();
      });
    }
    await provider.forceFlush();
    const spans = exporter.getFinishedSpans();
    assert.equal(spans.length, 5);
    assert(spans.every(s => s.status.code === 2));
    assert.equal(spans[0].attributes['http.response.status_code'], 400);
    assert.equal(spans[0].attributes['error.type'], 'ApiError');
    assert(spans.slice(1).every(s => s.attributes['http.response.status_code'] === undefined));
    assert(spans.every(s => !JSON.stringify({attrs: s.attributes, status: s.status, events: s.events}).includes('PRIVATE_')));
    assert.equal(params.error, error);
    assert.equal(params.error.message, 'PRIVATE_ERROR');
    assert.deepEqual(params.llmRequest.contents, ['PRIVATE_INPUT']);
  } finally {
    await provider.shutdown();
    trace.disable(); context.disable(); propagation.disable();
  }
});
