const { test } = require('node:test');
const assert = require('node:assert/strict');
const { captureMCPResult, wrapMCPClient, MCP_MEDIA_DECODE_BUDGET } = require('../dist/src');
const { harness, PNG, WAV, PDF, identity, warnings } = require('./attachment-harness.cjs');
const image = () => ({ type: 'image', data: PNG.toString('base64'), mimeType: 'image/png' });

test('typed image/audio/nested resource bytes use the shared TLS uploader and exact span', async () => {
  const h = await harness();
  try {
    const response = { content: [image(), { type: 'audio', data: WAV.toString('base64'), mimeType: 'audio/wav' }, { type: 'resource', resource: { uri: 'file:///untrusted/secret.pdf', mimeType: 'application/pdf', blob: PDF.toString('base64') } }] };
    const promise = Promise.resolve(response), args = [{ name: 'screenshot' }, undefined, { signal: new AbortController().signal }];
    const client = { callTool(...received) { assert.equal(this, client); assert.deepEqual(received, args); return promise; } };
    const wrapped = wrapMCPClient(client, { tracerProvider: h.provider });
    assert.equal(wrapMCPClient(wrapped), wrapped);
    assert.equal(wrapMCPClient(client, { tracerProvider: h.provider }), wrapped, 'one proxy per raw client');
    assert.equal(wrapped.callTool(...args), promise);
    assert.equal(await promise, response);
    await h.manager.flush();
    assert.equal(h.uploads.length, 3);
    assert.deepEqual(h.uploads.map(b => b.toString('base64')).sort(), [PNG, WAV, PDF].map(b => b.toString('base64')).sort());
    const span = h.exporter.getFinishedSpans()[0];
    assert.equal(span.name, 'mcp.tools/call');
    for (const record of h.records) {
      assert.equal(record.association.parent.span_id, span.spanContext().spanId);
      assert.equal(record.direction, 'output');
      assert.equal(record.purpose, 'tool_result');
    }
    assert.equal(JSON.parse(span.attributes['fi.attachment.refs']).length, 3);
    assert.ok(!JSON.stringify(span.attributes).includes('untrusted'));
    assert.ok(!JSON.stringify(h.records).includes('secret.pdf'));
    for (const request of h.requests.filter(r => /\/(content|complete)$/.test(r.path))) assert.equal(request.headers.authorization, 'Bearer synthetic-write-capability');
  } finally { await h.close(); }
});

for (const [label, options, capture] of [
  ['disabled', { enabled: false }],
  ...['hideInputs', 'hideOutputs', 'hideInputMessages', 'hideOutputMessages', 'hideInputText', 'hideOutputText', 'hideInputImages', 'piiRedaction'].map(flag => [flag, { traceConfig: { [flag]: true } }]),
  ['captureContent', {}, { captureContent: false }], ['muted', {}, { muted: true }], ['temporaryChat', {}, { temporaryChat: true }],
  ['recordInputs', {}, { recordInputs: false }], ['recordOutputs', {}, { recordOutputs: false }],
]) test(`privacy ${label}: no decoding, source getters, reference or network upload`, async () => {
  const h = await harness({ manager: options });
  try {
    let reads = 0;
    const part = { type: 'image', mimeType: 'image/png', get data() { reads++; throw Error('secret'); } };
    const handles = captureMCPResult(h.manager, identity, { content: [part] }, 'tools/call', capture);
    await h.manager.flush();
    assert.equal(reads, 0); assert.equal(h.uploads.length, 0); assert.equal(h.records.length, 0);
    assert.notEqual(handles[0].admission, 'queued');
    if (label === 'disabled') assert.equal(h.requests.length, 0);
  } finally { await h.close(); }
});

test('resource read bytes are captured; links, structuredContent and arbitrary paths are never dereferenced', async () => {
  const h = await harness();
  try {
    let uriReads = 0;
    const resource = { mimeType: 'application/pdf', blob: PDF.toString('base64'), get uri() { uriReads++; throw Error('secret'); } };
    captureMCPResult(h.manager, identity, { contents: [resource] }, 'resources/read');
    captureMCPResult(h.manager, identity, { content: [{ type: 'resource_link', get uri() { throw Error('secret'); } }, { type: 'text', text: 'file:///x' }], structuredContent: { screenshot: image() } });
    await h.manager.flush();
    assert.equal(uriReads, 0); assert.equal(h.uploads.length, 1); assert.deepEqual(h.uploads[0], PDF);
  } finally { await h.close(); }
});

test('malformed, MIME-mislabelled, oversized and excess parts cannot consume unbounded decode work', async () => {
  const h = await harness();
  try {
    const parts = [
      { type: 'image', mimeType: 'audio/wav', data: PNG.toString('base64') },
      { ...image(), data: '!!!!' }, { ...image(), data: 'a'.repeat(MCP_MEDIA_DECODE_BUDGET * 2) },
    ];
    captureMCPResult(h.manager, identity, { content: parts });
    await h.manager.flush(); assert.equal(h.uploads.length, 0);
    captureMCPResult(h.manager, identity, { content: Array.from({ length: 100 }, image) });
    await h.manager.flush(); assert.equal(h.uploads.length, 64);
  } finally { await h.close(); }
});

test('cancellation and synchronous failures retain original error and produce no media', async () => {
  const h = await harness();
  try {
    const controller = new AbortController(), error = new Error('synthetic abort');
    const rejected = Promise.reject(error);
    const client = { callTool(_request, _schema, options) { assert.equal(options.signal, controller.signal); return rejected; }, readResource() { throw error; } };
    const wrapped = wrapMCPClient(client, { tracerProvider: h.provider });
    controller.abort();
    assert.equal(wrapped.callTool({}, undefined, { signal: controller.signal }), rejected);
    await assert.rejects(rejected, e => e === error);
    assert.throws(() => wrapped.readResource(), e => e === error);
    await h.manager.flush(); assert.equal(h.uploads.length, 0);
    assert.equal(h.exporter.getFinishedSpans().length, 2);
  } finally { await h.close(); }
});

test('error media and stricter integration flags cannot bypass manager policy', async () => {
  const h = await harness({ manager: { traceConfig: { hideErrorDetails: true } } });
  try {
    let reads = 0;
    const part = { type: 'image', mimeType: 'image/png', get data() { reads++; return PNG.toString('base64'); } };
    captureMCPResult(h.manager, identity, { content: [part], isError: true });
    const wrapped = wrapMCPClient({ callTool: () => Promise.resolve({ content: [part] }) }, { tracerProvider: h.provider, traceConfig: { hideInputImages: true } });
    await wrapped.callTool(); await h.manager.flush(); assert.equal(reads, 0); assert.equal(h.uploads.length, 0);
  } finally { await h.close(); }
});

test('policy tightening after enqueue discards decoded bytes before upload', async () => {
  const h = await harness();
  try {
    const [handle] = captureMCPResult(h.manager, identity, { content: [image()] });
    h.manager.updatePolicy({ enabled: false });
    await h.manager.flush(); assert.equal(h.uploads.length, 0); assert.notEqual((await handle.completion).status, 'validating');
  } finally { await h.close(); }
});

test('synchronous non-thenable result is returned unchanged and recorded as success', async () => {
  const h = await harness();
  try {
    const response = { content: [image()] };
    const wrapped = wrapMCPClient({ callTool: () => response }, { tracerProvider: h.provider });
    assert.equal(wrapped.callTool({ name: 'sync' }), response);
    await h.manager.flush();
    const [span] = h.exporter.getFinishedSpans();
    assert.equal(span.status.code, 1);
    assert.equal(h.uploads.length, 1);
  } finally { await h.close(); }
});

// Counts preflights on the instance; a media read asserts a preflight for that part already ran (design section 1).
function spyPreflight(manager) {
  const original = manager.preflight.bind(manager), state = { calls: 0, reasons: [] };
  manager.preflight = (target, options) => { const ticket = original(target, options); state.calls++; state.reasons.push(ticket.reason); return ticket; };
  return state;
}
const PDF100K = Buffer.concat([PDF, Buffer.alloc(100 * 1024)]);

test('every media part is preflighted before its encoded value is read, and read at most once', async () => {
  const h = await harness();
  try {
    const spy = spyPreflight(h.manager); const reads = [];
    const tracked = (value, owner, field) => Object.defineProperty(owner, field, { enumerable: true, get() { reads.push(spy.calls); return value; } });
    const parts = [
      tracked(PNG.toString('base64'), { type: 'image', mimeType: 'image/png' }, 'data'),
      tracked(WAV.toString('base64'), { type: 'audio', mimeType: 'audio/wav' }, 'data'),
      { type: 'resource', resource: tracked(PDF.toString('base64'), { uri: 'file:///x', mimeType: 'application/pdf' }, 'blob') },
    ];
    const handles = captureMCPResult(h.manager, identity, { content: parts });
    await h.manager.flush();
    assert.deepEqual(reads, [1, 2, 3], 'each value is read once, after its own preflight');
    assert.deepEqual(spy.reasons, ['queued', 'queued', 'queued']);
    assert.deepEqual(handles.map(x => x.admission), ['queued', 'queued', 'queued']);
    assert.equal(h.uploads.length, 3);
    // Transferred for validation under strict F01 response checks (no unsupported_version).
    for (const x of handles) assert.equal((await x.completion).status, 'validating');
  } finally { await h.close(); }
});

test('a negotiated upload limit below the decode budget still captures small media at its exact size', async () => {
  const h = await harness({ limits: { max_upload_bytes: 100 * 1024 } });
  try {
    const spy = spyPreflight(h.manager);
    const big = { type: 'resource', resource: { mimeType: 'application/pdf', blob: Buffer.concat([PDF100K, Buffer.alloc(50 * 1024)]).toString('base64') } };
    const handles = captureMCPResult(h.manager, identity, { content: [image(), big] });
    await h.manager.flush();
    assert.deepEqual(spy.reasons, ['too_large', 'queued', 'too_large', 'too_large']);
    assert.deepEqual(handles.map(x => x.admission), ['queued', 'too_large']);
    assert.equal(h.uploads.length, 1); assert.deepEqual(h.uploads[0], PNG);
  } finally { await h.close(); }
});

test('a nearly used owned-byte budget still admits small media at its exact size', async () => {
  const h = await harness({ manager: { maxOwnedBytes: 200 * 1024 } });
  try {
    const spy = spyPreflight(h.manager);
    const pdf = { type: 'resource', resource: { mimeType: 'application/pdf', blob: PDF100K.toString('base64') } };
    const handles = captureMCPResult(h.manager, identity, { content: [pdf, image()] });
    assert.deepEqual(spy.reasons, ['too_large', 'queued', 'queue_full', 'queued']);
    await h.manager.flush();
    assert.deepEqual(handles.map(x => x.admission), ['queued', 'queued']);
    assert.equal(h.uploads.length, 2);
  } finally { await h.close(); }
});

test('an admitted part with malformed bytes returns its reservation and uploads nothing', async () => {
  const h = await harness();
  try {
    const handles = captureMCPResult(h.manager, identity, { content: [{ ...image(), data: '!!!!' }, { ...image(), data: 'QUJD' + '=' }] });
    assert.equal(handles.length, 0);
    const status = h.manager.status();
    assert.equal(status.openTickets, 0); assert.equal(status.ownedBytes, 0);
    await h.manager.flush(); assert.equal(h.uploads.length, 0); assert.equal(h.records.length, 0);
  } finally { await h.close(); }
});

test('the loopback service is accepted as a V1 F01 peer: no FI_ATTACHMENTS_* warning in this process', async () => {
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(warnings, []);
});
