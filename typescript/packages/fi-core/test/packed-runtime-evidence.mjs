// A single Node app can create spans through CJS and export them through ESM.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import * as esm from '@traceai/fi-core';
const cjs = createRequire(import.meta.url)('@traceai/fi-core');
for (const [author, boundary] of [[cjs, esm], [esm, cjs]]) {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanLimits: { attributeCountLimit: 6 }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const receipt = author === cjs ? 'aaaa1111-1111-4111-8111-111111111111' : 'bbbb1111-1111-4111-8111-111111111111';
    const tracer = new author.FITracer({ tracer: provider.getTracer('mixed-formats') });
    const clean = {'fi.context.session_id': receipt, 'fi.skill.bytes': 123, 'fi.skill.requested_label': 'production'};
    tracer.startSpan('verified', {attributes:{...clean, ...Object.fromEntries(Array.from({length:12},(_,i)=>[`input.field.${i}`, 'ordinary']))}}).end();
    const raw = exporter.getFinishedSpans()[0]; assert.ok(raw.droppedAttributesCount > 0);
    const first = boundary.privacyFilter().span(raw);
    for (const view of [first, author.privacyFilter().span(first)]) for (const [key, value] of Object.entries(clean)) assert.equal(view.attributes[key], value, key);
    first.attributes['fi.skill.requested_label'] = 'unverified'; // valid slug, but not the attested value
    const changed = author.privacyFilter().span(first);
    assert.notEqual(changed.attributes['fi.skill.requested_label'], 'unverified');
  } finally { await provider.shutdown(); }
}
console.log('PASS: mixed CJS/ESM FI admission, projection, detached re-export and changed value');
