/*
 * Deferred-target preflight (late-bound wire spans), the consumed-stream reservation clamp and the shutdown reason.
 * Synthetic data and in-memory doubles only (sdk-streaming-design sections 1, 6 and 8; rows T-DT, T-SR, T-SD).
 */
const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const { core, managerV1, span, clock } = require('./attachments-v1.cjs');
const { identity } = require('./attachments-fixtures.cjs');
const { AttachmentManager, AttachmentDeferredCorrelation, AttachmentCorrelation } = core;
const live = setInterval(() => {}, 1000); // Explicit test owner, not an SDK keepalive.
after(() => clearInterval(live));
const tick = () => new Promise(resolve => setImmediate(resolve));
const MiB = 1024 * 1024;
const wav = { mimeType: 'audio/wav', direction: 'input', purpose: 'model_input' };
const mp3 = { mimeType: 'audio/mpeg', direction: 'output', purpose: 'model_output' };
const ctx2 = { traceId: 'fedcba9876543210fedcba9876543210', spanId: 'fedcba9876543210', traceFlags: 1 };
const dashed = hex => `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
/** A Blob whose reads are counted: deferral must never read it before bind. */
function spyBlob(bytes, type = 'audio/wav') {
  const blob = new Blob([bytes], { type }); let reads = 0;
  for (const name of ['arrayBuffer', 'stream', 'text', 'bytes']) {
    const original = blob[name]; if (typeof original !== 'function') continue;
    blob[name] = function (...args) { reads++; return original.apply(this, args); };
  }
  const slice = blob.slice.bind(blob); blob.slice = (...args) => { reads++; return slice(...args); };
  return { blob, reads: () => reads };
}

// ---- T-DT deferred target ---------------------------------------------------------------------------------------
test('T-DT-01 Blob admitted before the span exists: nothing sent or read until bind; then uploaded with the bound parent and projected', async () => {
  const f = await managerV1();
  const target = f.manager.deferredCorrelation();
  assert.ok(target instanceof AttachmentDeferredCorrelation); assert.ok(target instanceof AttachmentCorrelation); assert.equal(target.pending, true);
  const { blob, reads } = spyBlob(new Uint8Array([1, 2, 3, 4, 5]));
  const ticket = f.manager.preflight(target, { ...wav, maxBytes: blob.size, sourceKind: 'Blob' });
  assert.equal(ticket.reason, 'queued');
  const h = ticket.attach({ kind: 'Blob', data: blob }, { messageIndex: 0, partIndex: 1 });
  assert.equal(h.admission, 'queued');
  await f.manager.flush(50); await tick();
  assert.deepEqual(f.ops().filter(op => op !== 'bootstrap'), [], 'no request before bind');
  assert.equal(reads(), 0, 'the Blob is retained, never read, before bind');
  assert.equal(f.manager.status().ownedBytes, 5);
  const s = span(ctx2);
  assert.equal(target.bind(s), true); assert.equal(target.pending, false);
  assert.equal((await h.completion).status, 'validating');
  assert.deepEqual([...f.contents.get(h.occurrenceId)], [1, 2, 3, 4, 5]);
  const request = f.requests.find(r => r.occurrence_id === h.occurrenceId);
  assert.equal(request.association.parent.trace_id, dashed(ctx2.traceId)); assert.equal(request.association.parent.span_id, ctx2.spanId);
  const refs = JSON.parse(s.attrs['fi.attachment.refs']);
  assert.equal(refs.length, 1); assert.equal(refs[0].occurrence_id, h.occurrenceId); assert.equal(refs[0].part_index, 1);
  assert.equal(s.attrs['fi.attachment.schema_version'], 1);
  assert.equal(f.manager.correlation(s), target, 'the bound target is the span\'s single correlation');
  await f.manager.flush(); assert.equal(f.manager.status().ownedBytes, 0);
  await f.manager.shutdown();
});
test('T-DT-02 consumed stream finished before bind: bounded chunks held, uploaded at bind byte-identical', async () => {
  const f = await managerV1();
  const target = f.manager.deferredCorrelation();
  const stream = f.manager.startAttachmentStream(target, { ...mp3, maxBytes: 64 });
  assert.equal(stream.reason, 'queued');
  const buffer = new Uint8Array([7, 8, 9]); assert.equal(stream.tryWrite(buffer), true); buffer.fill(0); assert.equal(stream.tryWrite(new Uint8Array([10])), true);
  const h = stream.finish(); assert.equal(h.admission, 'queued'); assert.equal(h.occurrenceId, stream.occurrenceId);
  await f.manager.flush(50); await tick();
  assert.equal(f.ops().includes('initialize'), false);
  assert.equal(f.manager.status().ownedBytes, 4, 'the reservation shrank to the consumed length and is still held');
  assert.equal((await f.manager.flush(1)).unfinished, 1, 'a parked capture counts as unfinished; flush does not wait for it');
  const s = span(ctx2); target.bind(s);
  assert.equal((await h.completion).status, 'validating');
  assert.deepEqual([...f.contents.get(h.occurrenceId)], [7, 8, 9, 10]);
  assert.equal(JSON.parse(s.attrs['fi.attachment.refs'])[0].occurrence_id, h.occurrenceId);
  await f.manager.shutdown();
});
test('T-DT-03 stream still open at bind finishes normally; an over-cap deferred stream uploads and projects nothing', async () => {
  const f = await managerV1(); const target = f.manager.deferredCorrelation(); const s = span(ctx2);
  const stream = f.manager.startAttachmentStream(target, { ...mp3, maxBytes: 4 });
  stream.tryWrite(new Uint8Array([1, 2])); target.bind(s); stream.tryWrite(new Uint8Array([3]));
  const h = stream.finish(); assert.equal((await h.completion).status, 'validating'); assert.deepEqual([...f.contents.get(h.occurrenceId)], [1, 2, 3]);
  const t2 = f.manager.deferredCorrelation(); const s2 = span(identity);
  const over = f.manager.startAttachmentStream(t2, { ...mp3, maxBytes: 4 });
  over.tryWrite(new Uint8Array(3)); assert.equal(over.tryWrite(new Uint8Array(3)), false); assert.equal(over.reason, 'too_large');
  t2.bind(s2); assert.equal((await over.finish().completion).status, 'too_large');
  assert.equal(s2.attrs['fi.attachment.refs'], undefined); await f.manager.flush(); assert.equal(f.manager.status().ownedBytes, 0);
  await f.manager.shutdown();
});
test('T-DT-04 bind timeout (fake clock): pending captures fail correlation_unavailable at the next entry point; no timer armed; nothing sent on a late bind', async () => {
  const c = clock(); const f = await managerV1({}, { clock: c });
  const timers = []; const original = global.setTimeout;
  global.setTimeout = (...args) => { timers.push(args[1]); return original(...args); };
  let target, h, stream, ticket;
  try {
    target = f.manager.deferredCorrelation({ bindTimeoutMs: 1000 });
    h = f.manager.preflight(target, { ...wav, maxBytes: 3 }).attach({ kind: 'Bytes', data: new Uint8Array([1, 2, 3]) });
    stream = f.manager.startAttachmentStream(target, { ...mp3, maxBytes: 64, idleTtlMs: 300000 }); stream.tryWrite(new Uint8Array(5));
    ticket = f.manager.preflight(target, { ...wav, maxBytes: 8, ttlMs: 30000 });
  } finally { global.setTimeout = original; }
  assert.equal(timers.length, 0, 'deferral armed a timer');
  assert.equal(f.manager.status().ownedBytes, 3 + 64 + 8);
  c.advance(1001);
  const st = f.manager.status();
  assert.equal(st.ownedBytes, 0); assert.equal(st.openStreams, 0); assert.equal(st.openTickets, 0);
  assert.equal((await h.completion).status, 'correlation_unavailable');
  assert.equal(stream.reason, 'correlation_unavailable');
  assert.equal(ticket.attach({ kind: 'Bytes', data: new Uint8Array(2) }).admission, 'correlation_unavailable');
  assert.equal(f.manager.preflight(target, { ...wav, maxBytes: 2 }).reason, 'correlation_unavailable', 'an expired unbound target admits nothing');
  const s = span(ctx2); target.bind(s);
  await f.manager.flush(50);
  assert.equal(f.ops().includes('initialize'), false); assert.equal(s.attrs['fi.attachment.refs'], undefined);
  await f.manager.shutdown(1);
});
test('T-DT-05 cancel(): pending captures and streams end correlation_unavailable, budget returned, later captures refused, bind refused', async () => {
  const f = await managerV1(); const target = f.manager.deferredCorrelation();
  const h = f.manager.attach(target, { kind: 'Bytes', data: new Uint8Array([1]) }, wav);
  const stream = f.manager.startAttachmentStream(target, { ...mp3, maxBytes: 16 }); stream.tryWrite(new Uint8Array(2));
  const ticket = f.manager.preflight(target, { ...wav, maxBytes: 4 });
  target.cancel();
  assert.equal((await h.completion).status, 'correlation_unavailable'); assert.equal(stream.reason, 'correlation_unavailable');
  assert.equal(f.manager.status().ownedBytes, 0); assert.equal(f.manager.status().openTickets, 0);
  let read = false; assert.equal(ticket.attach({ kind: 'Bytes', get data() { read = true; return new Uint8Array(1); } }).admission, 'correlation_unavailable'); assert.equal(read, false);
  assert.equal(f.manager.preflight(target, { ...wav, maxBytes: 1 }).reason, 'correlation_unavailable');
  assert.equal(target.bind(span(ctx2)), false);
  await f.manager.flush(50); assert.equal(f.ops().includes('initialize'), false);
  await f.manager.shutdown();
});
test('T-DT-06 manager shutdown before bind: correlation_unavailable, nothing uploaded, a later bind sends nothing', async () => {
  const f = await managerV1(); const target = f.manager.deferredCorrelation();
  const h = f.manager.preflight(target, { ...wav, maxBytes: 2 }).attach({ kind: 'Bytes', data: new Uint8Array([1, 2]) });
  const s = f.manager.startAttachmentStream(target, { ...mp3, maxBytes: 8 }); s.tryWrite(new Uint8Array(1)); const sh = s.finish();
  const result = await f.manager.shutdown(200);
  assert.equal((await h.completion).status, 'correlation_unavailable'); assert.equal((await sh.completion).status, 'correlation_unavailable');
  assert.equal(result.unfinished, 0); assert.equal(f.manager.status().ownedBytes, 0);
  target.bind(span(ctx2)); await tick();
  assert.equal(f.ops().includes('initialize'), false);
});
test('T-DT-07 policy is evaluated again at bind: environment kill switch, tightened trace config, invalidate', async () => {
  // (a) A deny that arrives with no policy-epoch event (the environment is re-read): only the bind-time check sees it.
  let env = {};
  const a = await managerV1({}, {}, { environment: () => env });
  const ta = a.manager.deferredCorrelation();
  const ha = a.manager.preflight(ta, { ...wav, maxBytes: 1 }).attach({ kind: 'Bytes', data: new Uint8Array([1]) });
  env = { FI_ATTACHMENTS_ENABLED: 'false' };
  const sa = span(ctx2); ta.bind(sa);
  assert.equal((await ha.completion).status, 'disabled'); assert.equal(sa.attrs['fi.attachment.refs'], undefined);
  await a.manager.flush(50); assert.equal(a.ops().includes('initialize'), false); assert.equal(a.manager.status().ownedBytes, 0);
  // (b) Tightened trace config between commit and bind.
  const b = await managerV1(); const tb = b.manager.deferredCorrelation();
  const hb = b.manager.startAttachmentStream(tb, { ...wav, maxBytes: 8 }); hb.tryWrite(new Uint8Array(1)); const hbh = hb.finish();
  b.manager.updatePolicy({ traceConfig: { hideInputs: true } });
  assert.equal((await hbh.completion).status, 'policy_denied'); tb.bind(span(ctx2)); await b.manager.flush(50);
  assert.equal(b.ops().includes('initialize'), false);
  // (c) Revocation.
  const c = await managerV1(); const tc = c.manager.deferredCorrelation();
  const hc = c.manager.attach(tc, { kind: 'Bytes', data: new Uint8Array([1]) }, wav);
  c.manager.invalidate('deleted'); assert.equal((await hc.completion).status, 'deleted');
  for (const f of [a, b, c]) await f.manager.shutdown(1);
});
test('T-DT-08 unsampled or non-recording span at bind: unsampled, nothing uploaded', async () => {
  const f = await managerV1(); const target = f.manager.deferredCorrelation();
  const h = f.manager.attach(target, { kind: 'Bytes', data: new Uint8Array([1]) }, wav);
  target.bind({ ...ctx2, traceFlags: 0 });
  assert.equal((await h.completion).status, 'unsampled');
  const t2 = f.manager.deferredCorrelation(); const h2 = f.manager.attach(t2, { kind: 'Bytes', data: new Uint8Array([1]) }, wav);
  const ended = span(ctx2); ended.end(); t2.bind(ended);
  assert.equal((await h2.completion).status, 'unsampled');
  await f.manager.flush(50); assert.equal(f.ops().includes('initialize'), false); assert.equal(f.manager.status().ownedBytes, 0);
  await f.manager.shutdown();
});
test('T-DT-09 pre-bind bytes are capped at half the owned budget; bound captures keep their headroom', async () => {
  const f = await managerV1({}, {}, { maxOwnedBytes: 1000 });
  const target = f.manager.deferredCorrelation();
  const held = f.manager.preflight(target, { ...wav, maxBytes: 400 }); assert.equal(held.reason, 'queued');
  assert.equal(f.manager.preflight(target, { ...wav, maxBytes: 101 }).reason, 'queue_full', 'over the pre-bind cap');
  assert.equal(f.manager.startAttachmentStream(f.manager.deferredCorrelation(), { ...mp3, maxBytes: 101 }).reason, 'queue_full');
  assert.equal(f.manager.attach(target, { kind: 'Bytes', data: new Uint8Array(101) }, wav).admission, 'queue_full');
  assert.equal(f.manager.preflight(identity, { ...wav, maxBytes: 500 }).reason, 'queued', 'a bound capture still fits');
  held.release();
  assert.equal(f.manager.preflight(target, { ...wav, maxBytes: 100 }).reason, 'queued');
  await f.manager.shutdown(1);
});
test('T-DT-10 one reference list per span: a span that already has a correlation keeps it and the target forwards to it', async () => {
  const f = await managerV1(); const s = span(ctx2);
  const first = f.manager.attach(s, { kind: 'Bytes', data: new Uint8Array([1]) }, { ...wav, partIndex: 0 });
  const target = f.manager.deferredCorrelation();
  const late = f.manager.attach(target, { kind: 'Bytes', data: new Uint8Array([2]) }, { ...wav, partIndex: 1 });
  target.bind(s);
  await first.completion; assert.equal((await late.completion).status, 'validating');
  const refs = JSON.parse(s.attrs['fi.attachment.refs']);
  assert.deepEqual(refs.map(r => r.occurrence_id).sort(), [first.occurrenceId, late.occurrenceId].sort());
  assert.equal(target.references().length, 2);
  const after = f.manager.preflight(target, { ...wav, maxBytes: 1 }); assert.equal(after.reason, 'queued');
  after.attach({ kind: 'Bytes', data: new Uint8Array([3]) }, { partIndex: 2 });
  assert.equal(JSON.parse(s.attrs['fi.attachment.refs']).length, 3);
  await f.manager.shutdown();
});
test('T-DT-11 scope: another manager\'s target, owned files, recordings and imports are not deferred (correlation_unavailable)', async () => {
  const f = await managerV1(); const g = await managerV1();
  const foreign = g.manager.deferredCorrelation();
  assert.equal(f.manager.preflight(foreign, { ...wav, maxBytes: 1 }).reason, 'correlation_unavailable');
  const target = f.manager.deferredCorrelation();
  assert.equal(f.manager.preflight(target, { ...wav, maxBytes: 1, sourceKind: 'OwnedRecordingFile' }).reason, 'correlation_unavailable');
  assert.equal(f.manager.startRecording(target, { format: { sampleRateHz: 8000 }, layout: 'mixed', tracks: [{ channels: [0], direction: 'mixed', role: 'mixed' }], operation: 'observe_consumed' }).reason, 'correlation_unavailable');
  let resolved = false;
  const imported = await f.manager.importCompletedRecording(target, { provider: 'livekit', providerRef: 'egress-1' }, { resolver: { resolve: async () => { resolved = true; } } });
  assert.equal(imported.reason, 'correlation_unavailable'); assert.equal(resolved, false);
  assert.equal(f.manager.status().ownedBytes, 0);
  await f.manager.shutdown(); await g.manager.shutdown();
});
test('T-DT-12 denials before admission never touch the source; a plain unbound correlation is still correlation_unavailable', async () => {
  const f = await managerV1({}, {}, { traceConfig: { hideInputs: true } });
  const target = f.manager.deferredCorrelation(); let read = false;
  const t = f.manager.preflight(target, { ...wav, maxBytes: 4 }); assert.equal(t.reason, 'policy_denied');
  assert.equal(t.attach({ kind: 'Bytes', get data() { read = true; return new Uint8Array(1); } }).admission, 'policy_denied'); assert.equal(read, false);
  const g = await managerV1();
  assert.equal(g.manager.preflight(new AttachmentCorrelation(), { ...wav, maxBytes: 1 }).reason, 'correlation_unavailable');
  assert.equal(g.manager.preflight(undefined, { ...wav, maxBytes: 1 }).reason, 'correlation_unavailable');
  await f.manager.shutdown(1); await g.manager.shutdown(1);
});
test('T-DT-13 disabled: inert deferred target, preflight disabled, bind schedules nothing, no timers/finalizers/listeners', async () => {
  const saved = { st: global.setTimeout, fr: global.FinalizationRegistry }; let timers = 0; let registry = 0;
  const listenersBefore = process.eventNames().map(n => process.listenerCount(n)).reduce((a, b) => a + b, 0);
  try {
    global.setTimeout = (...args) => { timers++; return saved.st(...args); };
    global.FinalizationRegistry = class extends saved.fr { constructor(...a) { super(...a); registry++; } };
    for (const m of [new AttachmentManager({ environment: () => ({}) }), new core.DisabledAttachmentManager()]) {
      const target = m.deferredCorrelation({ bindTimeoutMs: 10 });
      assert.equal(m.preflight(target, { ...wav, maxBytes: 4 }).reason, 'disabled');
      assert.equal(m.startAttachmentStream(target, { ...mp3, maxBytes: 4 }).reason, 'disabled');
      const s = span(ctx2); assert.equal(target.bind(s), true); assert.equal(s.writes, 0);
      target.cancel();
    }
  } finally { global.setTimeout = saved.st; global.FinalizationRegistry = saved.fr; }
  assert.equal(timers, 0); assert.equal(registry, 0);
  assert.equal(process.eventNames().map(n => process.listenerCount(n)).reduce((a, b) => a + b, 0), listenersBefore);
});
test('T-DT-14 bind is synchronous and never throws; the transport runs only from the queue', async () => {
  let sync = true; let calledSync = false;
  const f = await managerV1(base => ({ initialize: async (...args) => { if (sync) calledSync = true; return base.initialize(...args); } }));
  const target = f.manager.deferredCorrelation();
  const h = f.manager.attach(target, { kind: 'Bytes', data: new Uint8Array([1]) }, wav);
  const throwing = { spanContext: () => ctx2, isRecording: () => true, setAttribute() { throw Error('host'); } };
  assert.doesNotThrow(() => target.bind(throwing)); sync = false;
  assert.equal(calledSync, false); assert.equal((await h.completion).status, 'validating');
  await f.manager.shutdown();
});

// ---- T-SR consumed-stream reservation = min(requested, negotiated single-upload cap) ---------------------------------
test('T-SR-01 server max_upload_bytes below 4 MiB: a 4 MiB stream request is admitted at the server cap', async () => {
  const f = await managerV1({}, { limits: { max_upload_bytes: 1 * MiB } });
  const stream = f.manager.startAttachmentStream(identity, { ...mp3, maxBytes: 4 * MiB });
  assert.equal(stream.reason, 'queued'); assert.equal(f.manager.status().ownedBytes, 1 * MiB, 'reservation = min(4 MiB, max_upload_bytes)');
  assert.equal(stream.tryWrite(new Uint8Array(512 * 1024)), true);
  const h = stream.finish(); assert.equal((await h.completion).status, 'validating'); assert.equal(f.contents.get(h.occurrenceId).length, 512 * 1024);
  const over = f.manager.startAttachmentStream(identity, { ...mp3, maxBytes: 4 * MiB });
  assert.equal(over.tryWrite(new Uint8Array(MiB)), true); assert.equal(over.tryWrite(new Uint8Array(1)), false);
  assert.equal(over.reason, 'too_large', 'a body over the server cap is discarded whole');
  // The explicit ticket API keeps exact semantics: the caller asked for more than the cap.
  assert.equal(f.manager.preflight(identity, { ...mp3, maxBytes: 4 * MiB }).reason, 'too_large');
  // A request below the cap is unchanged.
  await f.manager.flush();
  const small = f.manager.startAttachmentStream(identity, { ...mp3, maxBytes: 1000 }); assert.equal(f.manager.status().ownedBytes, 1000); small.abort();
  await f.manager.shutdown();
});
test('T-SR-02 the clamp never admits what the lattice denies, and needs a session', async () => {
  const pending = await managerV1({ bootstrap: () => new Promise(() => {}) }, { ready: false });
  assert.equal(pending.manager.startAttachmentStream(identity, { ...mp3, maxBytes: 4 * MiB }).reason, 'bootstrap_pending');
  const denied = await managerV1({}, { limits: { max_upload_bytes: MiB } }, { traceConfig: { hideOutputs: true } });
  assert.equal(denied.manager.startAttachmentStream(identity, { ...mp3, maxBytes: 4 * MiB }).reason, 'policy_denied');
  const bad = await managerV1({}, { limits: { max_upload_bytes: MiB } });
  assert.equal(bad.manager.startAttachmentStream(identity, { ...mp3, maxBytes: 0 }).reason, 'too_large');
  assert.equal(bad.manager.startAttachmentStream(identity, { ...mp3, maxBytes: 1.5 }).reason, 'too_large');
  for (const f of [pending, denied, bad]) await f.manager.shutdown(1);
});

// ---- T-SD shutdown keeps an earlier invalidate reason ---------------------------------------------------------------
test('T-SD-01 invalidate(policy_denied) then shutdown(): state and admissions stay policy_denied, not incomplete', async () => {
  for (const reason of ['policy_denied', 'deleted', 'expired']) {
    const f = await managerV1();
    f.manager.invalidate(reason);
    assert.equal(f.manager.status().state, reason);
    await f.manager.shutdown(100);
    assert.equal(f.manager.status().state, reason, `shutdown overwrote ${reason}`);
    assert.equal(f.manager.preflight(identity, { ...wav, maxBytes: 1 }).reason, reason);
    assert.equal(f.manager.attach(identity, { kind: 'Bytes', data: new Uint8Array(1) }, wav).admission, 'incomplete', 'attach after shutdown is incomplete, as before');
  }
  const plain = await managerV1(); await plain.manager.shutdown(100);
  assert.equal(plain.manager.status().state, 'incomplete', 'a plain shutdown still reports incomplete');
});
