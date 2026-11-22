import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { ROOT_CONTEXT } from '@opentelemetry/api';
import { InMemorySpanExporter, SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import * as esm from '@traceai/fi-core';
const cjs = createRequire(import.meta.url)('@traceai/fi-core');
for (const [owner, supplier] of [[esm, cjs], [cjs, esm]]) {
  const order = [];
  const observed = (module, name) => {
    const p = new module.FutureAGIRuntimeAttributesSpanProcessor();
    const start = p.onStart.bind(p);
    p.onStart = (...args) => { order.push(name); start(...args); };
    return p;
  };
  const supplied = observed(supplier, 'supplied');
  const duplicate = observed(owner, 'duplicate');
  const generic = '77777777-2222-4222-8222-222222222222';
  const ordinary = { onStart(span) { order.push('observer'); assert.equal(span.attributes['fi.skills.count'], 1); },
    onEnd() {}, async forceFlush() {}, async shutdown() {} };
  const exporter = new InMemorySpanExporter();
  const provider = owner.register({projectName: 'processor-test', setGlobalTracerProvider: false,
    spanProcessors: [ordinary, supplied, duplicate, new SimpleSpanProcessor(exporter)]});
  try {
    const parent = ROOT_CONTEXT.setValue(Symbol.for('futureagi.runtime.span_attributes.v1'), {v: 1,
      attrs: {'fi.skills.count': 1, 'fi.context.session_id': generic}});
    provider.getTracer('identity').startSpan('ordinary', {attributes: {note: generic}}, parent).end();
    assert.deepEqual(order, ['supplied', 'observer']);
    const [raw] = exporter.getFinishedSpans();
    assert.equal(raw.name, 'ordinary');
    assert.equal(raw.attributes['fi.skills.count'], 1);
    assert.ok(!JSON.stringify(owner.privacyFilter().span(raw)).includes(generic));
  } finally { await provider.shutdown(); }
}
