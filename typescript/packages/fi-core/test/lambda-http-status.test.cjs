const { test, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { createRequire } = require('node:module');
const load = createRequire(process.env.FI_CORE_PACKAGE_JSON || path.resolve(__dirname, '../package.json'));
for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
const { context, trace, SpanStatusCode } = load('@opentelemetry/api');
const { NodeTracerProvider } = load('@opentelemetry/sdk-trace-node');
const { InMemorySpanExporter, SimpleSpanProcessor } = load('@opentelemetry/sdk-trace-base');
const { traceLambdaHandler } = load('@traceai/fi-core');
const providers = [];
function memory() {
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  provider.register(); providers.push(provider);
  return { provider, exporter };
}
const invocation = () => ({ getRemainingTimeInMillis: () => 10000, callbackWaitsForEmptyEventLoop: false });
const events = {
  v1: { httpMethod: 'GET', requestContext: {}, headers: {} },
  v2: { version: '2.0', requestContext: { http: { method: 'GET' } }, headers: {} },
  alb: { requestContext: { elb: { targetGroupArn: 'synthetic' } }, headers: {} },
};
function call(completion, outcome, event, provider, error) {
  const handler = traceLambdaHandler(completion === 'promise' ? async () => {
    if (error) throw error;
    return outcome;
  } : (_event, _context, callback) => { queueMicrotask(() => callback(error, outcome)); }, { provider, completion });
  return completion === 'promise' ? handler(event, invocation()) : new Promise((resolve, reject) =>
    handler(event, invocation(), (err, result) => err ? reject(err) : resolve(result)));
}
afterEach(async () => {
  for (const provider of providers.splice(0)) await provider.shutdown();
  trace.disable(); context.disable();
});
for (const completion of ['promise', 'callback']) {
  for (const [kind, event] of Object.entries(events)) {
    for (const statusCode of [200, 404, 500, 503]) {
      test(`${completion} ${kind} records only HTTP status ${statusCode}`, async () => {
        const { provider, exporter } = memory();
        const result = { statusCode, get body() { throw new Error('private body must not be read'); },
          get headers() { throw new Error('private headers must not be read'); } };
        assert.equal(await call(completion, result, event, provider), result);
        const [span] = exporter.getFinishedSpans();
        assert.equal(exporter.getFinishedSpans().length, 1);
        assert.equal(span.attributes['http.response.status_code'], statusCode);
        assert.equal(span.status.code, statusCode >= 500 ? SpanStatusCode.ERROR : SpanStatusCode.UNSET);
        assert.equal(span.attributes['error.type'], statusCode >= 500 ? String(statusCode) : undefined);
        assert.equal(span.status.message, undefined);
        assert.deepEqual(span.events, []);
        assert.ok(!JSON.stringify(span.attributes).includes('private'));
      });
    }
  }
  for (const [name, event, result] of [
    ['queue result', { Records: [] }, { statusCode: 503 }],
    ['missing request context', { httpMethod: 'GET' }, { statusCode: 503 }],
    ['non-HTTP request context', { requestContext: { requestId: 'synthetic' } }, { statusCode: 503 }],
    ['string status', events.v2, { statusCode: '503' }],
    ['fractional status', events.v2, { statusCode: 500.5 }],
    ['out of range', events.v2, { statusCode: 600 }],
    ['below range', events.v2, { statusCode: 99 }],
    ['nonfinite', events.v2, { statusCode: NaN }],
    ['null result', events.v2, null],
    ['string result', events.v2, 'private result'],
    ['throwing status', events.v2, { get statusCode() { throw new Error('private status'); } }],
  ]) {
    test(`${completion} ignores ${name} without altering the result`, async () => {
      const { provider, exporter } = memory();
      assert.equal(await call(completion, result, event, provider), result);
      const [span] = exporter.getFinishedSpans();
      assert.equal(span.attributes['http.response.status_code'], undefined);
      assert.equal(span.attributes['error.type'], undefined);
      assert.equal(span.status.code, SpanStatusCode.UNSET);
    });
  }
  test(`${completion} preserves thrown exception identity and hides its contents`, async () => {
    const { provider, exporter } = memory();
    const error = new TypeError('private error');
    await assert.rejects(call(completion, undefined, events.v2, provider, error), e => e === error);
    const [span] = exporter.getFinishedSpans();
    assert.equal(span.status.code, SpanStatusCode.ERROR);
    assert.equal(span.attributes['error.type'], 'TypeError');
    assert.equal(span.attributes['http.response.status_code'], undefined);
    assert.ok(!JSON.stringify({ attributes: span.attributes, status: span.status, events: span.events }).includes('private'));
  });
}
