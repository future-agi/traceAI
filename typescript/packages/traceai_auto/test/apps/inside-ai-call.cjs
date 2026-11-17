// An application with its own OpenTelemetry SDK (app-otel.cjs: its own exporter
// and context manager) and one OpenAI call. Its own fetch for that call records
// the provider request with a span of its own, so that span is created inside
// the AI call. Nothing here loads traceAI: the test runs it with and without the
// preload, and prints the spans that the application's own exporter received,
// the ids of its request span and the traceparent of its provider request.
require('./app-otel.cjs');
const { trace, context, propagation } = require('@opentelemetry/api');
const OpenAI = require('openai');
const tracer = trace.getTracer('own-app');
// Application code in the request path of the AI call. With INSIDE_DEPTH=2 it also
// starts a second span of its own, for the attempt, inside the first one.
const depth = Number(process.env.INSIDE_DEPTH || 1);
// The traceparent that the application's own propagator gives its provider request.
// It is only recorded here; the request is sent without it.
let providerTraceparent = null, requestContext = null;
function fetchWithOwnSpan(url, init) {
  return tracer.startActiveSpan('app-provider-request', async span => {
    try {
      const carrier = {};
      propagation.inject(context.active(), carrier);
      providerTraceparent = carrier.traceparent ?? null;
      if (depth < 2) return await globalThis.fetch(url, init);
      return await tracer.startActiveSpan('app-provider-attempt', async attempt => {
        try { return await globalThis.fetch(url, init); } finally { attempt.end(); }
      });
    } finally { span.end(); }
  });
}
(async () => {
  const client = new OpenAI({ apiKey: 'mock-provider-key', baseURL: process.env.MOCK_URL + '/v1', fetch: fetchWithOwnSpan, maxRetries: 0 });
  await tracer.startActiveSpan('app-request', async request => {
    // Its ids, also when the application's sampler did not record it.
    const { traceId, spanId } = request.spanContext();
    requestContext = { traceId, spanId };
    try {
      // An application span outside any AI call.
      tracer.startSpan('app-before').end();
      // One AI call; its fetch creates the application span inside it.
      const response = await client.chat.completions.create({ model: 'gpt-fixture', messages: [{ role: 'user', content: 'hello' }] });
      if (response.choices[0].message.content !== 'hello world') throw new Error('Unexpected completion');
      // One more application span, after the call.
      tracer.startSpan('app-after').end();
    } finally { request.end(); }
  });
  process.on('exit', () => {
    console.log(JSON.stringify({ appSpans: globalThis.__app.spans.map(span => ({
      name: span.name, traceId: span.spanContext().traceId, spanId: span.spanContext().spanId,
      parentSpanId: span.parentSpanContext?.spanId ?? span.parentSpanId ?? null,
    })), request: requestContext, providerTraceparent }));
  });
})().catch(error => { console.error(error); process.exitCode = 1; });
