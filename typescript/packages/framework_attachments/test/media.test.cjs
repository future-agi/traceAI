const { test } = require('node:test');
const assert = require('node:assert/strict');
const { captureMediaMessages, captureMediaParts } = require('../dist/src/index.js');
const { fixture, PNG, URI } = require('./fixture.cjs');
const parts = {
  langchain: [{ type: 'image_url', image_url: { url: URI } }],
  llamaindex: [{ type: 'image_url', image_url: { url: URI } }],
  vercel: [{ type: 'image', image: PNG, mediaType: 'image/png' }],
  genkit: [{ media: { url: URI, contentType: 'image/png' } }],
};
for (const [dialect, content] of Object.entries(parts)) {
  test(`${dialect}: original bytes, occurrence correlation and idempotent boundary`, async () => {
    const f = await fixture();
    try {
      const span = f.tracer.startSpan('real-otel-span');
      const source = [{ role: 'user', content }];
      captureMediaMessages({ attachments: f.manager }, span, source, dialect, 'input');
      captureMediaMessages({ attachments: f.manager }, span, source, dialect, 'input');
      span.end(); await f.manager.flush();
      assert.equal(f.bodies.length, 1); assert.deepEqual(f.bodies[0], PNG);
      assert.equal(f.completed.length, 1);
      assert.equal(f.uploads[0].association.parent.trace_id.replaceAll('-', ''), span.spanContext().traceId);
      assert.equal(f.uploads[0].association.parent.span_id, span.spanContext().spanId);
      assert.equal(f.uploads[0].message_index, 0); assert.equal(f.uploads[0].part_index, 0);
      const attributes = f.exporter.getFinishedSpans()[0].attributes;
      const refs = JSON.parse(attributes['fi.attachment.refs']);
      assert.equal(refs.length, 1); assert.equal(refs[0].occurrence_id, f.uploads[0].occurrence_id);
      assert.ok(!JSON.stringify(attributes).includes(URI));
    } finally { await f.close(); }
  });
  for (const [name, config] of Object.entries({ disabled: { enabled: false }, hideInputs: { traceConfig: { hideInputs: true } },
    hideMessages: { traceConfig: { hideInputMessages: true } }, hideImages: { traceConfig: { hideInputImages: true } },
    hideText: { traceConfig: { hideInputText: true } }, pii: { traceConfig: { piiRedaction: true } },
    metadataRemoval: { serverPolicy: { require_metadata_removal: true } }, mediaRedaction: { serverPolicy: { require_media_redaction: true } },
  })) test(`${dialect}: ${name} causes no upload, decode or occurrence`, async () => {
    const f = await fixture(config); let decoded = 0; const atob = globalThis.atob;
    globalThis.atob = (...args) => { decoded++; return atob(...args); };
    try {
      const span = f.tracer.startSpan('denied');
      captureMediaMessages({ attachments: f.manager }, span, [{ role: 'user', content }], dialect, 'input');
      span.end(); await f.manager.flush();
      assert.equal(f.uploads.length, 0); assert.equal(f.bodies.length, 0); assert.equal(decoded, 0);
      assert.equal(f.exporter.getFinishedSpans()[0].attributes['fi.attachment.refs'], undefined);
      if (name === 'disabled') assert.equal(f.requests.length, 0);
    } finally { globalThis.atob = atob; await f.close(); }
  });
}
test('tool images intersect input/output policy; binding denial cannot be relaxed', async () => {
  for (const config of [{ traceConfig: { hideInputImages: true } }, { traceConfig: { hideOutputs: true } }, {}]) {
    const f = await fixture(config);
    try {
      const span = f.tracer.startSpan('tool');
      captureMediaParts({ attachments: f.manager, capture: { recordOutputs: false } }, span, parts.vercel, 'vercel', {
        direction: 'output', purpose: 'tool_result', capture: { modelInput: true, recordOutputs: true },
      });
      span.end(); await f.manager.flush(); assert.equal(f.uploads.length, 0);
    } finally { await f.close(); }
  }
});
test('no getters, paths, remote URLs, metadata or malformed/oversize data are recovered', async () => {
  const f = await fixture(); let getters = 0;
  try {
    const span = f.tracer.startSpan('unsupported');
    const values = [
      { type: 'image', get image() { getters++; throw new Error('private'); }, mediaType: 'image/png' },
      { type: 'image', image: 'https://provider.invalid/secret', mediaType: 'image/png' },
      { type: 'image', image: '/private/file.png', mediaType: 'image/png' },
      { type: 'image', image: 'a'.repeat(1500000), mediaType: 'image/png' },
      { type: 'image', image: 'invalid!', mediaType: 'image/png' },
      { type: 'image', image: URI, mediaType: 'application/pdf' },
    ];
    captureMediaParts({ attachments: f.manager }, span, values, 'vercel', { direction: 'input' });
    span.end(); await f.manager.flush(); assert.equal(f.uploads.length, 0); assert.equal(getters, 0);
  } finally { await f.close(); }
});
test('mutable bytes are detached synchronously and distinct message occurrences survive', async () => {
  const f = await fixture();
  try {
    const span = f.tracer.startSpan('lifetime'); const mutable = new Uint8Array(PNG);
    const content = [{ type: 'file', data: mutable, mediaType: 'image/png', filename: 'never-export.png' }];
    captureMediaMessages({ attachments: f.manager }, span, [{ role: 'user', content }, { role: 'user', content }], 'vercel', 'input');
    mutable.fill(0); span.end(); await f.manager.flush();
    assert.equal(f.bodies.length, 2); for (const body of f.bodies) assert.deepEqual(body, PNG);
    assert.notEqual(f.uploads[0].occurrence_id, f.uploads[1].occurrence_id);
    assert.ok(!JSON.stringify(f.uploads).includes('never-export'));
  } finally { await f.close(); }
});
test('integration-level trace config also wins over a permissive shared manager', async () => {
  const f = await fixture();
  try {
    const span = f.tracer.startSpan('local-hide');
    captureMediaMessages({ attachments: f.manager, traceConfig: { hideInputText: true } }, span, [{ content: parts.vercel }], 'vercel', 'input');
    span.end(); await f.manager.flush(); assert.equal(f.uploads.length, 0);
  } finally { await f.close(); }
});
