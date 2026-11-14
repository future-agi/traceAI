// An application with its own OpenTelemetry SDK (app-otel.cjs: its own exporter
// and context manager) and one OpenAI call. Its own fetch for that call records
// the provider request with a span of its own, so that span is created inside
// the AI call. Nothing here loads traceAI: the test runs it with and without the
// preload, and prints the spans that the application's own exporter received.
require('./app-otel.cjs');
const { trace } = require('@opentelemetry/api');
const OpenAI = require('openai');
const tracer = trace.getTracer('own-app');
// Application code in the request path of the AI call.
function fetchWithOwnSpan(url, init) {
  return tracer.startActiveSpan('app-provider-request', async span => {
    try { return await globalThis.fetch(url, init); } finally { span.end(); }
  });
}
(async () => {
  const client = new OpenAI({ apiKey: 'mock-provider-key', baseURL: process.env.MOCK_URL + '/v1', fetch: fetchWithOwnSpan, maxRetries: 0 });
  await tracer.startActiveSpan('app-request', async request => {
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
    })) }));
  });
})().catch(error => { console.error(error); process.exitCode = 1; });
