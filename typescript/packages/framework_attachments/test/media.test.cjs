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
// SDK design section 1 (T-PF-09 analog): every part is preflighted before its source is decoded or copied.
const warnings = [];
process.on('warning', warning => { if (String(warning.code ?? '').startsWith('FI_ATTACHMENTS_')) warnings.push(warning.code); });
test('preflight denies a part over the owned budget before any decode (queue_full, zero atob)', async () => {
  for (const [dialect, content] of Object.entries(parts)) {
    const f = await fixture({ maxOwnedBytes: 16 }); let decoded = 0; const atob = globalThis.atob;
    globalThis.atob = (...args) => { decoded++; return atob(...args); };
    try {
      const span = f.tracer.startSpan('budget');
      captureMediaMessages({ attachments: f.manager }, span, [{ role: 'user', content }], dialect, 'input');
      span.end(); await f.manager.flush();
      assert.equal(decoded, 0, dialect); assert.equal(f.uploads.length, 0, dialect); assert.equal(f.bodies.length, 0, dialect);
      const status = f.manager.status();
      assert.equal(status.openTickets, 0, dialect); assert.equal(status.ownedBytes, 0, dialect);
    } finally { globalThis.atob = atob; await f.close(); }
  }
});
test('every part goes through preflight + ticket.attach; manager.attach is never called', async () => {
  for (const [dialect, content] of Object.entries(parts)) {
    const f = await fixture(); const calls = { preflight: 0, attach: 0 };
    const preflight = f.manager.preflight.bind(f.manager);
    f.manager.preflight = (target, options) => { calls.preflight++; assert.equal(options.maxBytes, PNG.length, dialect); return preflight(target, options); };
    f.manager.attach = () => { calls.attach++; throw new Error('legacy attach path'); };
    try {
      const span = f.tracer.startSpan('ticket');
      captureMediaMessages({ attachments: f.manager }, span, [{ role: 'user', content }], dialect, 'input');
      span.end(); await f.manager.flush();
      assert.deepEqual(calls, { preflight: 1, attach: 0 }, dialect);
      assert.equal(f.bodies.length, 1, dialect); assert.deepEqual(f.bodies[0], PNG);
    } finally { await f.close(); }
  }
});
test('an admitted part that fails strict decode releases its ticket; later parts still upload', async () => {
  const f = await fixture();
  try {
    const span = f.tracer.startSpan('release');
    // Shape-valid length (8 chars) with an invalid alphabet: admitted by size, rejected by the strict decoder.
    captureMediaParts({ attachments: f.manager }, span, [{ type: 'image', image: 'invalid!', mediaType: 'image/png' },
      { type: 'image', image: PNG.toString('base64'), mediaType: 'image/png' }], 'vercel', { direction: 'input' });
    const status = f.manager.status();
    assert.equal(status.openTickets, 0);
    span.end(); await f.manager.flush();
    assert.equal(f.uploads.length, 1); assert.equal(f.uploads[0].part_index, 1); assert.deepEqual(f.bodies[0], PNG);
    assert.equal(f.manager.status().ownedBytes, 0);
  } finally { await f.close(); }
});
test('the fixture speaks the v1 wire: no legacy-transport or wire-rejected warning fired', async () => {
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(warnings, []);
});
test('Uint8Array parts are bounded by the manager, not the 1 MiB base64 decode cap', async () => {
  const f = await fixture();
  try {
    const span = f.tracer.startSpan('large-bytes');
    const large = new Uint8Array(1536 * 1024); large.set(PNG); large[large.length - 1] = 7;
    captureMediaParts({ attachments: f.manager }, span, [{ type: 'image', image: large, mediaType: 'image/png' },
      { type: 'image', image: Buffer.from(large).toString('base64'), mediaType: 'image/png' }], 'vercel', { direction: 'input' });
    span.end(); await f.manager.flush(10000);
    // The bytes part uploads byte-identical; the same media as base64 is over the decode cap and is skipped.
    assert.equal(f.uploads.length, 1); assert.equal(f.uploads[0].part_index, 0);
    assert.equal(f.bodies.length, 1); assert.ok(Buffer.from(large).equals(f.bodies[0]));
  } finally { await f.close(); }
});
