// An application with its own OpenTelemetry SDK (app-otel.cjs: its own exporter
// and context manager) that starts, in one trace and in the same tick, one OpenAI
// call and one plain fetch to the same chat route. The plain fetch has no AI
// integration of its own, so only wire capture can record it. Nothing here loads
// traceAI. It prints the spans that the application's own exporter received and the
// ids of its request span.
require('./app-otel.cjs');
const { trace } = require('@opentelemetry/api');
const OpenAI = require('openai');
const tracer = trace.getTracer('own-app');
let requestContext = null;
function plainFetch() {
  return globalThis.fetch(process.env.MOCK_URL + '/v1/chat/completions', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ model: 'gpt-fixture', messages: [{ role: 'user', content: 'a plain fetch beside the call' }] }) })
    .then(response => response.json());
}
(async () => {
  // The SDK sends its request through the global fetch, with no span of the application's around it.
  const client = new OpenAI({ apiKey: 'mock-provider-key', baseURL: process.env.MOCK_URL + '/v1', fetch: (url, init) => globalThis.fetch(url, init), maxRetries: 0 });
  await tracer.startActiveSpan('app-request', async request => {
    // Its ids, also when the application's sampler did not record it.
    const { traceId, spanId } = request.spanContext();
    requestContext = { traceId, spanId };
    try {
      // The OpenAI call starts first, so its AI span is live when the plain fetch starts.
      const [completion, plain] = await Promise.all([
        client.chat.completions.create({ model: 'gpt-fixture', messages: [{ role: 'user', content: 'hello' }] }),
        plainFetch(),
      ]);
      if (completion.choices[0].message.content !== 'hello world' || plain.choices[0].message.content !== 'hello world') throw new Error('Unexpected completion');
    } finally { request.end(); }
  });
  process.on('exit', () => {
    console.log(JSON.stringify({ appSpans: globalThis.__app.spans.map(span => ({
      name: span.name, traceId: span.spanContext().traceId, spanId: span.spanContext().spanId,
      parentSpanId: span.parentSpanContext?.spanId ?? span.parentSpanId ?? null,
    })), request: requestContext }));
  });
})().catch(error => { console.error(error); process.exitCode = 1; });
