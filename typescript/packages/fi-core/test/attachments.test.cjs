/* Component qualification only: synthetic bytes and a loopback attachment API. */
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { once } = require('node:events');
const { pathToFileURL } = require('node:url');
const path = require('node:path');
const { createRequire } = require('node:module');
const packageJson = process.env.FI_CORE_PACKAGE_JSON || path.resolve(__dirname, '../package.json');
const load = createRequire(packageJson);
const { AttachmentManager, AttachmentCorrelation, canonicalAttachmentTraceId, attachmentTraceUuid,
  evaluateAttachmentPolicy, createAttachmentHttpTransport, AttachmentTransportError } = load('@traceai/fi-core/attachments');
const { register, ProjectType } = load('@traceai/fi-core');
const { identity, ids, policy, limits, options, source, expiry, fake } = require('./attachments-fixtures.cjs');
async function manager(overrides = {}, transportOverrides = {}) {
  const fixture = fake(transportOverrides);
  const value = new AttachmentManager({ enabled: true, projectName: 'fixture', projectType: 'observe', transport: fixture.transport, ...overrides });
  assert.equal(await value.ready(), true);
  return { value, ...fixture };
}
const live = setInterval(() => {}, 1000); // Explicit test owner, not an SDK keepalive.
after(() => clearInterval(live));

test('OFF by default: no bootstrap, source access or reference metadata', async () => {
  const f = fake(); let read = false;
  const value = new AttachmentManager({ transport: f.transport });
  const result = value.attach(identity, { kind: 'Bytes', get data() { read = true; throw Error('synthetic-secret'); } }, options);
  assert.equal(result.admission, 'disabled'); assert.equal(read, false); assert.deepEqual(f.calls, []);
  assert.deepEqual(await result.completion, { status: 'disabled' }); assert.equal(JSON.stringify(result).includes('synthetic-secret'), false);
});

test('unknown bootstrap retains no bytes and never reads a denied source', async () => {
  const value = new AttachmentManager({ enabled: true, projectName: 'fixture', projectType: 'observe', transport: fake({ bootstrap: () => new Promise(() => {}) }).transport });
  let read = false;
  const h = value.attach(identity, { kind: 'Bytes', get data() { read = true; return new Uint8Array(1); } }, options);
  assert.equal(h.admission, 'bootstrap_pending'); assert.equal(read, false); assert.equal(value.status().ownedBytes, 0);
  await value.shutdown(1);
});

for (const [key, direction, mimeType, capture] of [
  ['hideInputs', 'input', 'audio/wav'], ['hideInputMessages', 'input', 'application/pdf'], ['hideInputText', 'input', 'image/png'],
  ['hideOutputs', 'output', 'audio/wav'], ['hideOutputMessages', 'output', 'image/png'], ['hideOutputText', 'mixed', 'audio/wav'],
  ['hideInputImages', 'output', 'image/png', { modelInput: true }], ['hideErrorDetails', 'output', 'audio/wav', { errorDetail: true }],
  ['piiRedaction', 'mixed', 'audio/wav'], ['piiRedaction', 'input', 'image/png'], ['piiRedaction', 'output', 'video/mp4'],
  ['piiRedaction', 'input', 'application/pdf'], ['piiRedaction', 'input', 'application/octet-stream'],
]) test(`no source access or upload with ${key}/${mimeType}`, async () => {
  const f = await manager({ traceConfig: { [key]: true } }); let read = false;
  const h = f.value.attach(identity, { kind: 'Bytes', get data() { read = true; throw Error('synthetic-secret'); } }, { mimeType, direction, capture });
  assert.equal(h.admission, key === 'piiRedaction' ? 'redaction_unavailable' : 'policy_denied');
  await f.value.flush(); assert.equal(read, false); assert.deepEqual(f.calls, ['bootstrap']); await f.value.shutdown();
});

test('environment restrictions win over explicit per-call and constructor allows', async () => {
  const f = await manager({ environment: () => ({ FI_HIDE_INPUTS: 'malformed' }), traceConfig: { hideInputs: false } });
  assert.equal(f.value.attach(identity, source(), options).admission, 'policy_denied');
  assert.equal(evaluateAttachmentPolicy({ enabled: true }, {}, policy, 'Bytes', options, { FI_ATTACHMENTS_ENABLED: 'false' }), 'disabled');
  await f.value.shutdown();
});

test('mutable ownership is detached, admission asynchronous, completion not readiness', async () => {
  const f = await manager(); const src = source();
  const h = f.value.attach(identity, src, options); src.data.fill(9);
  assert.deepEqual(f.calls, ['bootstrap']); assert.equal(h.admission, 'queued');
  assert.deepEqual(await h.completion, { status: 'validating' }); assert.deepEqual(f.body(), [82, 73, 70, 70, 1, 2, 3, 4]);
  assert.deepEqual(await f.value.flush(), { unfinished: 0, dropped: 0 }); assert.equal(f.value.status().ownedBytes, 0); await f.value.shutdown();
});

test('Blob read is independent; unknown stream, file and URL never read', async () => {
  const f = await manager(); const blob = new Blob([new Uint8Array([1, 2, 3])]);
  assert.equal((await f.value.attach(identity, { kind: 'Blob', data: blob }, options).completion).status, 'validating');
  for (const kind of ['RemoteUrl', 'LocalFile', 'OwnedRecordingFile', 'ProviderFile']) {
    const item = { kind, get url() { throw Error('must-not-read'); }, get path() { throw Error('must-not-read'); } };
    assert.notEqual(f.value.attach(identity, item, options).admission, 'queued');
  }
  let pulled = false;
  assert.equal(f.value.attach(identity, { kind: 'Bytes', data: { getReader() { pulled = true; } } }, options).admission, 'source_unavailable');
  assert.equal(pulled, false); await f.value.shutdown();
});

test('known sampled identity required and exact trace bits preserved', async () => {
  const f = await manager();
  for (const traceId of ['00000000000000000000000000000000', 'BAD', '0123456789ABCDEF0123456789ABCDEF']) assert.equal(canonicalAttachmentTraceId(traceId), undefined);
  const hex = 'ffffffffffffffffffffffffffffffff'; assert.equal(attachmentTraceUuid(canonicalAttachmentTraceId(hex)), 'ffffffff-ffff-ffff-ffff-ffffffffffff');
  assert.equal(f.value.attach({ ...identity, traceFlags: 0 }, source(), options).admission, 'unsampled');
  assert.equal(f.value.attach(new AttachmentCorrelation(), source(), options).admission, 'correlation_unavailable');
  await f.value.shutdown();
});

test('same-span references bounded to 64; ended span untouched by completion', async () => {
  const f = await manager(); let recording = true; let writes = 0; const attrs = {};
  const span = { spanContext: () => identity, isRecording: () => recording, setAttribute(k, v) { writes++; attrs[k] = v; } };
  const correlation = f.value.correlation(span); const handles = [];
  for (let i = 0; i < 70; i++) handles.push(f.value.attach(span, source(), options));
  assert.equal(JSON.parse(attrs['fi.attachment.refs']).length, 64); assert.ok(Buffer.byteLength(attrs['fi.attachment.refs']) <= 16384);
  recording = false; const before = writes;
  await Promise.all(handles.map(h => h.completion)); assert.equal(writes, before);
  assert.equal(correlation.snapshot().sampled, true); await f.value.shutdown();
});

test('memory/descriptors include active transfer and backpressure drops without copying', async () => {
  let release; const wait = new Promise(resolve => { release = resolve; });
  const f = await manager({ maxOwnedBytes: 8, maxDescriptors: 1 }, { upload: async () => { await wait; } });
  const h = f.value.attach(identity, source(), options); let read = false;
  assert.equal(f.value.attach(identity, { kind: 'Bytes', get data() { read = true; return new Uint8Array(8); } }, options).admission, 'queue_full');
  assert.equal(read, false); assert.equal(f.value.status().ownedBytes, 8);
  release(); await h.completion; await f.value.flush(); assert.equal(f.value.status().ownedBytes, 0);
  assert.equal(f.value.attach(identity, { kind: 'Bytes', data: new Uint8Array(9) }, options).admission, 'too_large');
  await f.value.shutdown();
});

test('policy tightening cancels queued bytes and does not resurrect after re-enable', async () => {
  const f = await manager(); const h = f.value.attach(identity, source(), options);
  f.value.updatePolicy({ traceConfig: { hideInputs: true } });
  assert.equal((await h.completion).status, 'policy_denied'); assert.equal(f.value.status().ownedBytes, 0);
  f.value.updatePolicy({ traceConfig: { hideInputs: false } }); await f.value.flush(); assert.deepEqual(f.calls, ['bootstrap']); await f.value.shutdown();
});

test('bounded retries recheck live policy and preserve occurrence IDs', async () => {
  let tries = 0; const requests = []; const f = fake();
  const fixture = await manager({}, { initialize: async (s, r, signal) => { requests.push(r.occurrence_id); if (++tries < 3) throw new AttachmentTransportError('failed', true); return f.transport.initialize(s, r, signal); } });
  const h = fixture.value.attach(identity, source(), options); await h.completion;
  assert.equal(tries, 3); assert.equal(new Set(requests).size, 1); await fixture.value.shutdown();
});

test('authorization failures never retry and exception bodies are absent', async () => {
  let tries = 0;
  const f = await manager({}, { initialize: async () => { tries++; throw new AttachmentTransportError('credential_scope_unsupported'); } });
  const h = f.value.attach(identity, source(), options); assert.deepEqual(await h.completion, { status: 'credential_scope_unsupported' }); assert.equal(tries, 1); await f.value.shutdown();
});

test('flush deadline and idempotent shutdown cancel active work', async () => {
  let aborted = false;
  const f = await manager({}, { upload: (_s, _u, _d, signal) => new Promise((resolve, reject) => signal.addEventListener('abort', () => { aborted = true; reject(Error('synthetic-secret')); }, { once: true })) });
  const h = f.value.attach(identity, source(), options);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal((await f.value.flush(5)).unfinished, 1);
  const closing = f.value.shutdown(5); assert.equal(f.value.shutdown(5), closing); await closing;
  assert.equal(aborted, true); assert.equal((await h.completion).status, 'incomplete');
  assert.equal(f.value.attach(identity, source(), options).admission, 'incomplete');
});

test('HTTP v1 loopback upload: stable identity, bounded response, no source URLs', async () => {
  const calls = []; const uploads = new Map(); let policyOverride;
  const server = http.createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const data = Buffer.concat(chunks); calls.push({ method: req.method, path: req.url, data, headers: req.headers });
    res.setHeader('content-type', 'application/json');
    if (req.url.endsWith('attachment-sessions')) {
      const body = JSON.parse(data); const fixture = await fake().transport.bootstrap(body);
      res.end(JSON.stringify({ ...fixture, policy: policyOverride ?? policy }));
    } else if (req.url.endsWith('/uploads')) {
      const body = JSON.parse(data); const upload = await fake().transport.initialize(null, body); uploads.set(upload.upload_id, upload);
      assert.equal(body.association.parent.trace_id, '01234567-89ab-cdef-0123-456789abcdef'); res.statusCode = 201; res.end(JSON.stringify(upload));
    } else if (req.url.endsWith('/content')) {
      assert.deepEqual([...data], [...source().data]); res.end(JSON.stringify({ upload_id: ids[0], state: 'pending_upload', reason: 'queued' }));
    } else { res.statusCode = 202; res.end(JSON.stringify({ upload_id: ids[0], state: 'validating', reason: 'binding_pending' })); }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const client = req => new Promise((resolve, reject) => {
    const request = http.request(`http://127.0.0.1:${server.address().port}${new URL(req.url).pathname}`, { method: req.method, headers: req.headers, signal: req.signal }, response => {
      const chunks = []; response.on('data', c => chunks.push(c)); response.on('end', () => resolve({ status: response.statusCode, body: Buffer.concat(chunks) }));
    }); request.on('error', reject); request.end(req.body);
  });
  try {
    const transport = createAttachmentHttpTransport({ endpoint: 'https://attachments.example', headers: { authorization: 'Bearer synthetic', cookie: 'never-forward' }, client });
    const f = await manager({ transport }); const h = f.value.attach(identity, source(), options);
    assert.equal((await h.completion).status, 'validating'); assert.equal(calls.length, 4); assert.equal(calls.some(c => c.headers.cookie), false);
    await f.value.shutdown();
    for (const traceConfig of [{ hideInputs: true }, { hideOutputs: true }, { piiRedaction: true }]) {
      const value = new AttachmentManager({ enabled: true, projectName: 'fixture', projectType: 'observe', transport, traceConfig });
      await value.ready(); const before = calls.length; assert.notEqual(value.attach(identity, source(), options).admission, 'queued');
      await value.flush(); assert.equal(calls.length, before); await value.shutdown();
    }
  } finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
});

test('transport blocks redirects and invalid origins', async () => {
  for (const endpoint of ['http://example.com', 'https://user:secret@example.com', 'https://example.com/path', 'https://example.com/?token=secret']) assert.throws(() => createAttachmentHttpTransport({ endpoint, headers: {} }), /endpoint_unconfigured/);
  const transport = createAttachmentHttpTransport({ endpoint: 'https://attachments.example', headers: {}, client: async () => ({ status: 302, body: Buffer.from('synthetic-secret') }) });
  // Redirects are never followed; a 3xx is an unmapped status: incomplete, not retried (design 0.5).
  await assert.rejects(transport.bootstrap({}, new AbortController().signal), error => error.message === 'incomplete' && !error.retryable);
});

test('CJS and ESM leaf export the public manager API', async () => {
  const esm = await import(pathToFileURL(path.resolve(path.dirname(packageJson), 'dist/esm/attachments/index.js')));
  assert.equal(esm.AttachmentManager, AttachmentManager); assert.equal(esm.AttachmentCorrelation, AttachmentCorrelation); assert.equal(typeof esm.createAttachmentHttpTransport, 'function');
});

test('provider owns one manager and flush/shutdown drain its lifecycle', async () => {
  const f = fake();
  const provider = register({ projectName: 'fixture', projectType: ProjectType.OBSERVE, setGlobalTracerProvider: false, spanProcessors: [], attachments: { enabled: true, transport: f.transport } });
  await provider.attachments.ready();
  const h = provider.attachments.attach(identity, source(), options); await provider.forceFlush(); assert.equal((await h.completion).status, 'validating');
  const first = provider.shutdown(); assert.equal(first, provider.shutdown()); await first;
});

test('late completion uses the stamped writer carrier and never mutates an ended span', async () => {
  const f = await manager(); const attributes = {}; let recording = true; let writes = 0;
  const span = { spanContext: () => identity, isRecording: () => recording, setAttribute(k, v) { attributes[k] = v; writes++; } };
  const correlation = f.value.correlation(span);
  // Design 0.8: a correlation snapshot alone stamps nothing; only an admitted capture on a live span does.
  assert.equal(writes, 0);
  recording = false; const before = writes;
  const h = f.value.attach(correlation, source(), { ...options, conversationId: 'conversation-1', turnId: 'turn-1', toolCallId: 'call-1' });
  assert.equal((await h.completion).status, 'validating'); assert.equal(writes, before); await f.value.shutdown();
});

test('untrusted metadata keys and malformed IDs are not retained or projected', async () => {
  const f = await manager();
  const bad = ['abc\n', 'has spaces', 'secret@example.com', '../path', 'x'.repeat(129)];
  for (const conversationId of bad) assert.equal(f.value.attach(identity, source(), { ...options, conversationId }).admission, 'policy_denied');
  assert.equal(canonicalAttachmentTraceId(identity.traceId + '\n'), undefined);
  assert.equal(f.value.attach({ ...identity, spanId: identity.spanId + '\n' }, source(), options).admission, 'correlation_unavailable');
  assert.equal(f.value.attach(identity, source(), { ...options, capture: { recordInputs: 'unknown' } }).admission, 'policy_denied');
  await f.value.shutdown();
});

test('upload response binds exact IDs, reservation and capability', async () => {
  for (const field of ['attachment_id', 'occurrence_id', 'reserved_bytes', 'write_capability']) {
    const t = fake(); const f = await manager({}, { initialize: async (s, r) => ({ ...await t.transport.initialize(s, r), [field]: field === 'reserved_bytes' ? 999 : 'wrong' }) });
    const h = f.value.attach(identity, source(), options); assert.equal((await h.completion).status, 'unsupported_version');
    assert.equal(f.calls.includes('upload'), false); await f.value.shutdown();
  }
});

test('successful response reader rejects oversized control data', async () => {
  const transport = createAttachmentHttpTransport({ endpoint: 'https://attachments.example', headers: {}, client: async () => ({ status: 200, body: new Uint8Array(65537) }) });
  await assert.rejects(transport.bootstrap({}, new AbortController().signal), error => error.message === 'unsupported_version' && !error.retryable);
});

test('provider trace options cannot weaken attachment-level privacy restrictions', async () => {
  const f = fake();
  const provider = register({ projectName: 'fixture', projectType: ProjectType.OBSERVE, setGlobalTracerProvider: false, spanProcessors: [],
    traceConfig: { hideInputs: false }, attachments: { enabled: true, transport: f.transport, traceConfig: { hideInputs: true } } });
  await provider.attachments.ready(); assert.equal(provider.attachments.attach(identity, source(), options).admission, 'policy_denied');
  await provider.shutdown(); assert.deepEqual(f.calls, ['bootstrap']);
});
