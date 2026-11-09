const { test } = require('node:test');
const assert = require('node:assert/strict');
const { sanitizeSpan, FiSanitizingExporter } = require('@traceai/apollo-graphql');
const { ExportResultCode } = require('@opentelemetry/core');
function fixture() {
  return {
    instrumentationScope: { name: '@opentelemetry/instrumentation-graphql' },
    attributes: { 'graphql.operation.name': 'Profile', 'graphql.source': 'query Profile', unknown: 'PRIVATE' },
    events: [
      { name: 'PRIVATE', attributes: { unknown: 'PRIVATE' } },
      { name: 'exception', attributes: { 'exception.type': 'Error', 'exception.message': 'PRIVATE_ERROR',
        'exception.stacktrace': 'PRIVATE_STACK', unknown: 'PRIVATE' } },
    ],
    links: [{ context: { traceId: 'trace', spanId: 'parent' }, attributes: { unknown: 'PRIVATE' } }],
    status: { code: 2, message: 'PRIVATE_ERROR' },
    spanContext() { return { traceId: 'trace', spanId: 'span' }; },
  };
}
test('scoped allowlist filters future attributes, event names and links without mutation', () => {
  const source = fixture();
  for (const captureContent of [false, true]) {
    const span = sanitizeSpan(source, { captureContent });
    assert.deepEqual(span.spanContext(), source.spanContext());
    assert.equal(span.attributes.unknown, undefined);
    assert.equal(span.attributes['graphql.source'], captureContent ? 'query Profile' : undefined);
    assert.equal(span.status.message, captureContent ? 'PRIVATE_ERROR' : undefined);
    assert.equal(span.events.length, 1);
    assert.equal(span.events[0].attributes['exception.stacktrace'], undefined);
    assert.equal(span.events[0].attributes.unknown, undefined);
    assert.deepEqual(span.links[0].attributes, {});
    assert.deepEqual(span.links[0].context, source.links[0].context);
  }
  assert.equal(source.attributes.unknown, 'PRIVATE');
  assert.equal(source.events[0].name, 'PRIVATE');
  const appSpan = { ...source, instrumentationScope: { name: 'application' } };
  assert.equal(sanitizeSpan(appSpan), appSpan);
});

test('sanitization failure never falls back to raw spans or payload-bearing diagnostics', () => {
  let calls = 0;
  const exporter = new FiSanitizingExporter({ export() { calls++; }, shutdown: async () => {} });
  const span = fixture();
  Object.defineProperty(span, 'attributes', { get() { throw new Error('PRIVATE'); } });
  let result;
  exporter.export([span], value => { result = value; });
  assert.equal(calls, 0);
  assert.equal(result.code, ExportResultCode.FAILED);
  assert.doesNotMatch(result.error.message, /PRIVATE/);
});

test('exporter freezes privacy options and delegates lifecycle', async () => {
  const options = { captureContent: false };
  let spans, flushed = 0, stopped = 0;
  const exporter = new FiSanitizingExporter({
    export(value, callback) { spans = value; callback({ code: ExportResultCode.SUCCESS }); },
    forceFlush: async () => { flushed++; }, shutdown: async () => { stopped++; },
  }, options);
  options.captureContent = true;
  exporter.export([fixture()], () => {});
  assert.equal(spans[0].attributes['graphql.source'], undefined);
  await exporter.forceFlush();
  const shutdown = exporter.shutdown();
  assert.equal(shutdown, exporter.shutdown());
  await shutdown;
  assert.equal(flushed, 1); assert.equal(stopped, 1);
});

test('shared policy is resolved once and PII redaction never mutates the original span', () => {
  const original = process.env.FI_HIDE_INPUTS;
  try {
    process.env.FI_HIDE_INPUTS = 'true';
    const options = { captureContent: true, piiRedaction: true, hideErrorDetails: false };
    let exported;
    const exporter = new FiSanitizingExporter({
      export(spans, callback) { exported = spans[0]; callback({ code: ExportResultCode.SUCCESS }); },
      shutdown: async () => {},
    }, options);
    process.env.FI_HIDE_INPUTS = 'false';
    options.piiRedaction = false;
    const span = fixture();
    span.name = 'review.person@example.test';
    span.status.message = 'review.person@example.test';
    span.events[1].attributes['exception.message'] = 'review.person@example.test';
    exporter.export([span], () => {});
    assert.equal(exported.attributes['graphql.source'], undefined);
    assert.equal(exported.name, '__REDACTED__');
    assert.equal(exported.attributes['graphql.operation.name'], '__REDACTED__');
    assert.equal(span.name, 'review.person@example.test');
    assert.equal(exported.status.message, '<EMAIL_ADDRESS>');
    assert.equal(exported.events[0].attributes['exception.message'], '<EMAIL_ADDRESS>');
    assert.equal(span.status.message, 'review.person@example.test');
    assert.equal(span.events[1].attributes['exception.message'], 'review.person@example.test');
  } finally {
    if (original === undefined) delete process.env.FI_HIDE_INPUTS;
    else process.env.FI_HIDE_INPUTS = original;
  }
});
