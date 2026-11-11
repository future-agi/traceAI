const { test } = require('node:test');
const assert = require('node:assert/strict');
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { observePipecatClient } = require('../dist/src/client.js');
const { createClient } = require('./transport.cjs');
const identity = () => ({ schemaVersion: 1,
  parent: { traceId: '01234567-89ab-cdef-0123-456789abcdef', spanId: '0123456789abcdef' },
  occurrenceId: '10000000-0000-4000-8000-000000000001', conversationId: '10000000-0000-4000-8000-000000000002' });

for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  test(`${name}: server recording stays on its parent without media or credentials`, async t => {
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    t.after(() => provider.shutdown());
    const { client, transport } = createClient(require(name));
    let current = identity();
    Object.defineProperty(current, 'sourceUrl', { get() { throw Error('synthetic-secret-must-not-read'); } });
    const stop = observePipecatClient(client, { tracer: provider.getTracer('recording-test'), serverRecording: () => current });
    t.after(stop);
    await client.connect(); await client.disconnect();
    const first = exporter.getFinishedSpans(); assert.ok(first.length > 0);
    for (const s of first) {
      assert.equal(s.attributes['pipecat.server_recording.status'], 'reference_only');
      assert.equal(s.links.length, 1);
      assert.equal(s.links[0].context.traceId, current.parent.traceId.replaceAll('-', ''));
      assert.equal(s.links[0].context.spanId, current.parent.spanId);
      assert.equal(s.links[0].attributes['pipecat.server_recording.occurrence_id'], current.occurrenceId);
      assert.equal(s.attributes['fi.attachment.refs'], undefined); // not a relink
    }
    // The app clears identity when its server session ends. Reconnect never falls
    // back to a prior session or to a bot-ready payload's untrusted identifiers.
    current = undefined;
    await client.connect(); await client.disconnect();
    for (const s of exporter.getFinishedSpans().slice(first.length)) {
      assert.equal(s.attributes['pipecat.server_recording.status'], 'source_unavailable');
      assert.equal(s.links.length, 0);
    }
    const sent = JSON.stringify(transport.sent);
    assert.ok(!sent.includes('10000000-0000')); assert.ok(!sent.includes('01234567'));
    stop(); assert.equal(client.listenerCount('connected'), 0);
  });

  for (const [caseName, supplied] of [
    ['missing', undefined], ['version', { ...identity(), schemaVersion: 2 }],
    ['zero trace', { ...identity(), parent: { ...identity().parent, traceId: '0'.repeat(32) } }],
    ['bad span', { ...identity(), parent: { ...identity().parent, spanId: 'secret=synthetic' } }],
    ['URL occurrence', { ...identity(), occurrenceId: 'https://invalid.example/?key=synthetic' }],
    ['participant name', { ...identity(), conversationId: 'synthetic-private-person' }],
  ]) test(`${name}: ${caseName} identity has only source_unavailable status`, async t => {
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    t.after(() => provider.shutdown());
    const { client } = createClient(require(name));
    const stop = observePipecatClient(client, { tracer: provider.getTracer('invalid-test'), serverRecording: () => supplied });
    t.after(stop); await client.connect(); await client.disconnect();
    for (const s of exporter.getFinishedSpans()) {
      assert.equal(s.attributes['pipecat.server_recording.status'], 'source_unavailable');
      assert.deepEqual(s.links, []); assert.ok(!JSON.stringify(s.attributes).includes('synthetic-private'));
    }
  });
}
