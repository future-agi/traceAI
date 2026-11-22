// A single Node app can create spans through CJS and export them through ESM.
// Runtime evidence is opt-in and SDK-authored: only values written through the
// privileged identity call, on a span whose provider has the runtime processor.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import * as esm from '@traceai/fi-core';
const cjs = createRequire(import.meta.url)('@traceai/fi-core');
const FILL = Symbol.for('futureagi.runtime.fill-agent-identity.v1');
for (const [author, boundary] of [[cjs, esm], [esm, cjs]]) {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanLimits: { attributeCountLimit: 6 },
    spanProcessors: [new author.FutureAGIRuntimeAttributesSpanProcessor(), new SimpleSpanProcessor(exporter)] });
  try {
    const id = author === cjs ? 'aaaa1111-1111-4111-8111-111111111111' : 'bbbb1111-1111-4111-8111-111111111111';
    const tracer = new author.FITracer({ tracer: provider.getTracer('mixed-formats') });
    const span = tracer.startSpan('verified', {attributes: Object.fromEntries(Array.from({length: 12}, (_, i) => [`input.field.${i}`, 'ordinary']))});
    span[FILL]('gen_ai.agent.name', 'planner');
    span[FILL]('gen_ai.agent.id', id);
    span.end();
    const raw = exporter.getFinishedSpans()[0]; assert.ok(raw.droppedAttributesCount > 0);
    const first = boundary.privacyFilter().span(raw);
    for (const view of [first, author.privacyFilter().span(first)]) {
      assert.equal(view.attributes['gen_ai.agent.name'], 'planner');
      assert.equal(view.attributes['gen_ai.agent.id'], id);
    }
    first.attributes['gen_ai.agent.name'] = 'unverified'; // valid slug, but not the attested value
    const changed = author.privacyFilter().span(first);
    assert.notEqual(changed.attributes['gen_ai.agent.name'], 'unverified');
  } finally { await provider.shutdown(); }
}
console.log('PASS: mixed CJS/ESM SDK-written identity evidence, projection, detached re-export and changed value');
