// Isolated consumer: the two boundaries must agree without trusting inherited metadata.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { ROOT_CONTEXT } from '@opentelemetry/api';
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import * as esm from '@traceai/fi-core';
const cjs = createRequire(import.meta.url)('@traceai/fi-core');
for (const [author, boundary] of [[esm, cjs], [cjs, esm]]) {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({spanProcessors: [new SimpleSpanProcessor(exporter)]});
  try {
    const tracer = new author.FITracer({tracer: provider.getTracer('receipt')});
    const key = 'fi.context.session_id';
    const generic = '55555555-1111-4111-8111-111111111111';
    const receipt = '66666666-2222-4222-8222-222222222222';
    const inherited = author.setAttributes(ROOT_CONTEXT, {[key]: generic});
    const opts = {attributes: {note: `context ${generic}`, ordinary: 'keep'}};
    tracer.startSpan('inherited', opts, inherited).end();
    tracer.startActiveSpan('active-inherited', opts, inherited, span => span.end());
    tracer.startSpan('explicit', {attributes: {[key]: receipt, note: receipt}}, inherited).end();
    const late = tracer.startSpan('late', {}, inherited);
    late.setAttribute(key, receipt); late.end();
    const secret = '99999999-3333-4333-8333-333333333333';
    tracer.startSpan('sensitive', {attributes: {[key]: secret, note: secret}}).end();
    const spans = exporter.getFinishedSpans();
    assert.equal(spans.length, 5);
    for (const [i, raw] of spans.slice(0, 4).entries()) {
      const view = boundary.privacyFilter().span(raw);
      for (const clean of [view, author.privacyFilter().span(view)]) {
        if (i < 2) {
          assert.equal(clean.attributes[key], '<redacted>');
          assert.ok(!JSON.stringify(clean).includes(generic));
          assert.equal(clean.attributes.ordinary, 'keep');
        } else assert.equal(clean.attributes[key], receipt);
      }
      if (i === 2) assert.equal(view.attributes.note, receipt);
    }
    assert.ok(!JSON.stringify(boundary.privacyFilter({}, [secret]).span(spans[4])).includes(secret));
  } finally { await provider.shutdown(); }
}
