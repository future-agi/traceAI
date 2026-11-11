/* Design qualification (sdk-streaming-design.md section 10). Synthetic data and in-memory doubles only. */
const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const { core, v1, managerV1, span, pcm, writeSeconds, readAll, clock, mono } = require('./attachments-v1.cjs');
const { identity, ids, policy, limits, source } = require('./attachments-fixtures.cjs');
const { AttachmentManager, AttachmentTransportError, attachmentWavHeader, attachmentSegmentFrames, attachmentFramesToMs, validAttachmentManifest,
  validAttachmentWire, createAttachmentHttpTransport, createAttachmentHttpTransportV1, attachmentHttpError, attachmentIdempotencyKey, AttachmentCorrelation } = core;
const live = setInterval(() => {}, 1000); // Explicit test owner, not an SDK keepalive.
after(() => clearInterval(live));
const hex = value => value.replace(/-/g, '');
const tick = () => new Promise(resolve => setImmediate(resolve));
const options = { mimeType: 'image/png', direction: 'input' };

// ---- 10.1 Preflight -----------------------------------------------------------------------------------
test('T-PF-01 disabled manager: preflight denies without touching any source', async () => {
  const m = new AttachmentManager({ transport: v1().transport });
  const t = m.preflight(identity, { mimeType: 'image/png', direction: 'input', maxBytes: 10 });
  assert.equal(t.reason, 'disabled'); assert.equal(t.admitted, false);
  let read = false; const h = t.attach({ kind: 'Bytes', get data() { read = true; return new Uint8Array(1); } });
  assert.equal(h.admission, 'disabled'); assert.equal(read, false);
});
test('T-PF-02 bootstrap pending: denied and nothing reserved', async () => {
  const f = await managerV1({ bootstrap: () => new Promise(() => {}) }, { ready: false });
  const t = f.manager.preflight(identity, { mimeType: 'image/png', direction: 'input', maxBytes: 100 });
  assert.equal(t.reason, 'bootstrap_pending'); assert.equal(f.manager.status().ownedBytes, 0); assert.equal(f.manager.status().openTickets, 0);
  await f.manager.shutdown(1);
});
test('T-PF-04 create_recording needs client AND server record_audio; T-PF-05 import is not recording', async () => {
  const off = await managerV1({}, {}, { recordAudio: false });
  assert.equal(off.manager.preflight(identity, { mimeType: 'audio/wav', direction: 'mixed', maxBytes: 10, operation: 'create_recording' }).reason, 'recording_disabled');
  assert.equal(off.manager.preflight(identity, { mimeType: 'audio/wav', direction: 'mixed', maxBytes: 10, operation: 'import_existing' }).reason, 'queued');
  const server = await managerV1({}, { policy: { record_audio: false } }, { recordAudio: true });
  assert.equal(server.manager.preflight(identity, { mimeType: 'audio/wav', direction: 'mixed', maxBytes: 10, operation: 'create_recording' }).reason, 'recording_disabled');
  const both = await managerV1({}, {}, { recordAudio: true });
  assert.equal(both.manager.preflight(identity, { mimeType: 'audio/wav', direction: 'mixed', maxBytes: 10, operation: 'create_recording' }).reason, 'queued');
  for (const f of [off, server, both]) await f.manager.shutdown(1);
});
test('T-PF-06 mixed direction with hideInputs; T-PF-07 computer_screenshot output with hideInputImages', async () => {
  const a = await managerV1({}, {}, { traceConfig: { hideInputs: true } });
  assert.equal(a.manager.preflight(identity, { mimeType: 'audio/wav', direction: 'mixed', maxBytes: 10 }).reason, 'policy_denied');
  const b = await managerV1({}, {}, { traceConfig: { hideInputImages: true } });
  assert.equal(b.manager.preflight(identity, { mimeType: 'image/png', direction: 'output', purpose: 'computer_screenshot', maxBytes: 10 }).reason, 'policy_denied');
  assert.equal(b.manager.preflight(identity, { mimeType: 'image/png', direction: 'output', purpose: 'model_output', maxBytes: 10 }).reason, 'queued');
  await a.manager.shutdown(1); await b.manager.shutdown(1);
});
test('T-PF-08 descriptors or bytes exhausted -> queue_full', async () => {
  const f = await managerV1({}, {}, { maxDescriptors: 1, maxOwnedBytes: 100 });
  const first = f.manager.preflight(identity, { ...options, maxBytes: 10 }); assert.equal(first.reason, 'queued');
  assert.equal(f.manager.preflight(identity, { ...options, maxBytes: 10 }).reason, 'queue_full');
  first.release();
  assert.equal(f.manager.preflight(identity, { ...options, maxBytes: 101 }).reason, 'too_large');
  const g = await managerV1({}, {}, { maxOwnedBytes: 100 });
  const held = g.manager.preflight(identity, { ...options, maxBytes: 90 }); assert.equal(held.reason, 'queued');
  assert.equal(g.manager.preflight(identity, { ...options, maxBytes: 20 }).reason, 'queue_full');
  held.release(); assert.equal(g.manager.status().ownedBytes, 0);
  await f.manager.shutdown(1); await g.manager.shutdown(1);
});
test('T-PF-11 ticket used twice -> conflict and the source is untouched', async () => {
  const f = await managerV1();
  const t = f.manager.preflight(identity, { ...options, maxBytes: 8 });
  assert.equal(t.attach({ kind: 'Bytes', data: new Uint8Array([1, 2, 3]) }).admission, 'queued');
  let read = false;
  assert.equal(t.attach({ kind: 'Bytes', get data() { read = true; return new Uint8Array(1); } }).admission, 'conflict'); assert.equal(read, false);
  await f.manager.shutdown();
});
test('T-PF-12 ticket TTL with a fake clock: expired, budget returned lazily, no timer armed', async () => {
  const c = clock(); const f = await managerV1({}, { clock: c });
  const timers = []; const original = global.setTimeout;
  global.setTimeout = (...args) => { timers.push(args[1]); return original(...args); };
  let t;
  try { t = f.manager.preflight(identity, { ...options, maxBytes: 64, ttlMs: 100 }); } finally { global.setTimeout = original; }
  assert.equal(timers.length, 0, 'preflight armed a timer');
  assert.equal(f.manager.status().ownedBytes, 64);
  c.advance(150);
  assert.equal(f.manager.status().ownedBytes, 0); assert.equal(f.manager.status().openTickets, 0);
  assert.equal(t.attach({ kind: 'Bytes', data: new Uint8Array(4) }).admission, 'expired');
  await f.manager.shutdown(1);
});
test('T-PF-13 policy tightened between preflight and attach: new denial, zero reads', async () => {
  const f = await managerV1();
  const t = f.manager.preflight(identity, { ...options, maxBytes: 8 });
  f.manager.updatePolicy({ traceConfig: { hideInputs: true } });
  let read = false;
  assert.equal(t.attach({ kind: 'Bytes', get data() { read = true; return new Uint8Array(1); } }).admission, 'policy_denied');
  assert.equal(read, false); assert.equal(f.manager.status().ownedBytes, 0); await f.manager.shutdown(1);
});
test('T-PF-14 actual length above the ticket -> too_large, no copy; T-PF-15 mutable buffer above syncCopyBytes', async () => {
  const f = await managerV1({}, {}, { syncCopyBytes: 16 });
  const t = f.manager.preflight(identity, { ...options, maxBytes: 4 });
  assert.equal(t.attach({ kind: 'Bytes', data: new Uint8Array(5) }).admission, 'too_large');
  assert.equal(f.manager.status().ownedBytes, 0);
  const big = f.manager.preflight(identity, { ...options, maxBytes: 64 });
  assert.equal(big.attach({ kind: 'Bytes', data: new Uint8Array(32) }).admission, 'too_large');
  const blob = f.manager.preflight(identity, { ...options, maxBytes: 64, sourceKind: 'Blob' });
  const h = blob.attach({ kind: 'Blob', data: new Blob([new Uint8Array(32)]) });
  assert.equal(h.admission, 'queued'); assert.equal((await h.completion).status, 'validating');
  await f.manager.shutdown();
});
test('preflight pattern: denied ticket means no decode happened (adapter shape from design 1.5)', async () => {
  const f = await managerV1({}, {}, { traceConfig: { hideInputImages: true } });
  let decoded = 0; const decode = () => { decoded++; return new Uint8Array(4); };
  const t = f.manager.preflight(identity, { mimeType: 'image/png', direction: 'input', purpose: 'model_input', maxBytes: 4 });
  if (t.admitted) t.attach({ kind: 'Bytes', data: decode() });
  assert.equal(decoded, 0); await f.manager.shutdown(1);
});

// ---- 10.2 Streaming recordings ----------------------------------------------------------------------------
test('T-ST-01..04 worked vectors: segment frames, bytes and milliseconds', () => {
  assert.equal(attachmentSegmentFrames(24000, 2), 720000); assert.equal(44 + 720000 * 2, 1440044);
  assert.equal(attachmentSegmentFrames(48000, 4), 1440000); assert.equal(44 + 1440000 * 4, 5760044);
  assert.equal(attachmentSegmentFrames(48000, 16), 524256); assert.equal(44 + 524256 * 16, 8388140); assert.equal(attachmentFramesToMs(524256, 48000), 10922);
  assert.equal(attachmentFramesToMs(10000, 44100, true), 227);
  const h = attachmentWavHeader(1, 24000, 1440000); const v = new DataView(h.buffer);
  assert.equal(Buffer.from(h.subarray(0, 4)).toString(), 'RIFF'); assert.equal(v.getUint32(4, true), 1440036); assert.equal(Buffer.from(h.subarray(8, 16)).toString(), 'WAVEfmt ');
  assert.deepEqual([v.getUint32(16, true), v.getUint16(20, true), v.getUint16(22, true), v.getUint32(24, true), v.getUint32(28, true), v.getUint16(32, true), v.getUint16(34, true)], [16, 1, 1, 24000, 48000, 2, 16]);
  assert.equal(Buffer.from(h.subarray(36, 40)).toString(), 'data'); assert.equal(v.getUint32(40, true), 1440000);
});
test('T-ST-01 24 kHz mono 30 s: one 1 440 044-byte segment with an exact header; T-ST-20 carrier stamp, no references', async () => {
  const f = await managerV1(); const s = span();
  const w = f.manager.startRecording(s, mono(24000)); assert.equal(w.reason, 'queued');
  assert.equal(s.attrs['fi.attachment.refs'], '[]'); assert.equal(s.attrs['fi.attachment.schema_version'], 1);
  assert.equal(s.attrs['fi.attachment.capture_session_id'], f.manager.sessionId); assert.equal(s.attrs['fi.attachment.correlation_nonce'], f.manager.nonce);
  assert.equal(validAttachmentWire('SpanAttributes', s.attrs), true);
  await writeSeconds(f.manager, w, 24000, 30);
  const result = await w.finish(10000);
  assert.equal(result.state, 'published'); assert.equal(result.captureComplete, true);
  const sizes = f.calls.filter(c => c.op === 'putContent').map(c => c.size); assert.deepEqual(sizes, [1440044]);
  const body = [...f.contents.values()][0]; assert.deepEqual([...body.subarray(0, 44)], [...attachmentWavHeader(1, 24000, 1440000)]);
  const manifest = f.manifests.at(-1).body;
  assert.deepEqual(manifest.segments.map(s => [s.index, s.start_ms, s.duration_ms]), [[0, 0, 30000]]);
  assert.equal(manifest.duration_ms, 30000); assert.equal(validAttachmentWire('RecordingManifest', manifest), true);
  assert.equal(s.attrs['fi.attachment.refs'], '[]', 'segments are never projected as references');
  const request = f.requests[0];
  assert.equal(request.recording_id, w.recordingId); assert.equal(request.segment_index, 0); assert.equal(request.declared_bytes, 1440044);
  assert.equal(validAttachmentWire('UploadRequest', request), true);
  await f.manager.shutdown();
});
test('T-ST-02 24 kHz mono 61 s: 1 440 044 / 1 440 044 / 48 044, duration 61 000, capture_complete', async () => {
  const f = await managerV1();
  const w = f.manager.startRecording(identity, mono(24000));
  await writeSeconds(f.manager, w, 24000, 61);
  const result = await w.finish(10000);
  assert.deepEqual(f.calls.filter(c => c.op === 'putContent').map(c => c.size), [1440044, 1440044, 48044]);
  const manifest = f.manifests.at(-1).body;
  assert.equal(manifest.duration_ms, 61000); assert.equal(manifest.capture_complete, true); assert.deepEqual(manifest.gaps, []);
  assert.deepEqual(manifest.segments.map(s => s.duration_ms), [30000, 30000, 1000]); assert.equal(result.state, 'published');
  await f.manager.shutdown();
});
test('T-ST-03 44.1 kHz mono 10 000 frames: 20 044 bytes, duration 227', async () => {
  const f = await managerV1();
  const w = f.manager.startRecording(identity, mono(44100));
  assert.equal(w.tryWrite(pcm(10000)), true);
  const result = await w.finish(10000);
  assert.deepEqual(f.calls.filter(c => c.op === 'putContent').map(c => c.size), [20044]);
  assert.equal(f.manifests.at(-1).body.duration_ms, 227); assert.equal(f.manifests.at(-1).body.segments[0].duration_ms, 227); assert.equal(result.captureComplete, true);
  await f.manager.shutdown();
});
test('T-ST-05 negotiated segment_duration_ms 10 000 and segment_bytes 1 MiB are honoured', async () => {
  const f = await managerV1({}, { limits: { segment_duration_ms: 10000, segment_bytes: 1048576 } });
  const w = f.manager.startRecording(identity, mono(24000));
  await writeSeconds(f.manager, w, 24000, 25, { flushEvery: 5 });
  await w.finish(10000);
  const sizes = f.calls.filter(c => c.op === 'putContent').map(c => c.size);
  assert.deepEqual(sizes, [480044, 480044, 240044]); assert.ok(sizes.every(s => s <= 1048576));
  assert.ok(f.manifests.at(-1).body.segments.every(s => s.duration_ms <= 10000));
  await f.manager.shutdown();
});
const twoTracks = { format: { sampleRateHz: 16000 }, layout: 'separate_tracks', operation: 'observe_consumed',
  tracks: [{ channels: [0], direction: 'input', role: 'user' }, { channels: [1], direction: 'output', role: 'assistant' }] };
test('T-ST-06 separate tracks with one track starved for 5 s; T-ST-07 track_id rules', async () => {
  const f = await managerV1();
  const w = f.manager.startRecording(identity, twoTracks);
  for (let i = 0; i < 250; i++) { w.tryWrite(pcm(320), 0); w.tryWrite(pcm(320), 1); } // 5 s each
  for (let i = 0; i < 250; i++) w.tryWrite(pcm(320), 0); // track 1 starved for 5 s
  const result = await w.finish(10000);
  const m = f.manifests.at(-1).body;
  assert.equal(validAttachmentManifest(m), true); assert.equal(validAttachmentWire('RecordingManifest', m), true);
  assert.ok(m.segments.every(s => s.track_id) && m.gaps.every(g => g.track_id));
  const user = m.tracks[0].track_id; const bot = m.tracks[1].track_id;
  assert.deepEqual(m.segments.filter(s => s.track_id === user).map(s => [s.start_ms, s.duration_ms]), [[0, 10000]]);
  assert.deepEqual(m.segments.filter(s => s.track_id === bot).map(s => [s.start_ms, s.duration_ms]), [[0, 5000]]);
  assert.deepEqual(m.gaps.map(g => [g.track_id === bot, g.start_ms, g.duration_ms, g.reason]), [[true, 5000, 5000, 'source_unavailable']]);
  assert.equal(result.state, 'partial'); assert.equal(m.capture_complete, false);
  const mutated = JSON.parse(JSON.stringify(m)); delete mutated.gaps[0].track_id;
  assert.equal(validAttachmentManifest(mutated), false, 'F01 separate-track-needs-track-identity');
  await f.manager.shutdown();
});
test('T-ST-08 stereo: two tracks [0] and [1], channels 2; a [0],[0] mutation fails locally', async () => {
  const f = await managerV1();
  const w = f.manager.startRecording(identity, { ...twoTracks, layout: 'stereo' });
  assert.equal(w.reason, 'queued');
  for (let i = 0; i < 50; i++) assert.equal(w.tryWrite(pcm(320, 2)), true);
  await w.finish(10000);
  const m = f.manifests.at(-1).body;
  assert.equal(m.channels, 2); assert.deepEqual(m.tracks.map(t => t.channels), [[0], [1]]); assert.ok(m.segments.every(s => !('track_id' in s)));
  assert.equal(f.requests[0].direction, 'mixed'); assert.equal(validAttachmentManifest(m), true);
  const mutated = JSON.parse(JSON.stringify(m)); mutated.tracks[1].channels = [0]; assert.equal(validAttachmentManifest(mutated), false);
  const mixedMutation = JSON.parse(JSON.stringify(m)); mixedMutation.segments[0].track_id = m.tracks[0].track_id; assert.equal(validAttachmentManifest(mixedMutation), false);
  await f.manager.shutdown();
});
test('T-ST-09 budget exhausted mid-recording: tryWrite false, queue_full gap of the exact duration, no exception', async () => {
  const f = await managerV1({}, {}, { maxOwnedBytes: 32000 }); // 1 s of 16 kHz mono
  const w = f.manager.startRecording(identity, mono(16000));
  const a = await writeSeconds(f.manager, w, 16000, 1, { flushEvery: 0 }); assert.equal(a.rejected, 0);
  const b = await writeSeconds(f.manager, w, 16000, 1, { flushEvery: 0 }); assert.equal(b.accepted, 0);
  await w.finish(10000);
  const m = f.manifests.at(-1).body;
  assert.deepEqual(m.gaps.map(g => [g.start_ms, g.duration_ms, g.reason]), [[1000, 1000, 'queue_full']]);
  await f.manager.shutdown();
});
test('T-ST-10 window full (blocked transport): queue_full gaps, at most windowSegments sealed segments held', async () => {
  const f = await managerV1(base => ({ putContent: (_s, _u, _src, signal) => new Promise((_, reject) => signal.addEventListener('abort', () => reject(new AttachmentTransportError('incomplete')), { once: true })) }),
    { limits: { segment_duration_ms: 1000 } });
  const w = f.manager.startRecording(identity, mono(16000));
  const r = await writeSeconds(f.manager, w, 16000, 5, { flushEvery: 0 });
  assert.ok(r.rejected > 0);
  assert.ok(f.manager.status().ownedBytes <= 2 * (32000 + 44) + 262144, `owned ${f.manager.status().ownedBytes}`);
  const result = await w.finish(500); assert.ok(['partial', 'unpublished'].includes(result.state));
  await f.manager.shutdown(100);
});
test('T-ST-11 bootstrap pending at start, then ready: leading bootstrap_pending gap and no retained bytes before ready', async () => {
  let release; const gate = new Promise(resolve => { release = resolve; });
  const f = await managerV1(base => ({ bootstrap: async (body, key) => { await gate; return base.bootstrap(body, key); } }), { ready: false });
  const w = f.manager.startRecording(identity, mono(16000));
  assert.equal(w.reason, 'bootstrap_pending');
  await writeSeconds(f.manager, w, 16000, 1, { flushEvery: 0 });
  assert.equal(f.manager.status().ownedBytes, 0);
  release(); assert.equal(await f.manager.ready(), true);
  const r = await writeSeconds(f.manager, w, 16000, 1, { flushEvery: 0 }); assert.equal(r.rejected, 0);
  assert.equal(w.reason, 'queued');
  await w.finish(10000);
  const m = f.manifests.at(-1).body;
  assert.deepEqual(m.gaps.map(g => [g.start_ms, g.duration_ms, g.reason]), [[0, 1000, 'bootstrap_pending']]);
  assert.deepEqual(m.segments.map(s => [s.start_ms, s.duration_ms]), [[1000, 1000]]);
  await f.manager.shutdown();
});
test('T-ST-12 policy tightened mid-recording: writer terminal, bytes freed, final manifest ends with policy_denied', async () => {
  const f = await managerV1({}, { limits: { segment_duration_ms: 1000 } });
  const w = f.manager.startRecording(identity, mono(16000));
  await writeSeconds(f.manager, w, 16000, 2, { flushEvery: 1 });
  f.manager.updatePolicy({ traceConfig: { hideInputs: true } });
  assert.equal(w.tryWrite(pcm(320)), false); assert.equal(w.tryWrite(pcm(320)), false);
  await f.manager.flush(); assert.equal(f.manager.status().ownedBytes, 0);
  await w.finish(10000);
  const m = f.manifests.at(-1).body;
  assert.equal(m.segments.length, 2); assert.equal(m.gaps.at(-1).reason, 'policy_denied'); assert.equal(m.capture_complete, false);
  await f.manager.shutdown();
});
test('T-ST-13 recording cap (recording_duration_ms 60 000): truncated gap to the end, capture_complete false', async () => {
  const f = await managerV1({}, { limits: { recording_duration_ms: 60000 } });
  const w = f.manager.startRecording(identity, mono(8000));
  await writeSeconds(f.manager, w, 8000, 61);
  await w.finish(10000);
  const m = f.manifests.at(-1).body;
  assert.deepEqual(m.gaps.map(g => [g.start_ms, g.duration_ms, g.reason]), [[60000, 1000, 'truncated']]); assert.equal(m.capture_complete, false);
  await f.manager.shutdown();
});
test('T-ST-14 a segment failing after initialize is aborted and its index reused; a 409 on reuse closes partial', async () => {
  let failures = 0;
  const f = await managerV1(base => ({ putContent: async (s, u, src, sig) => {
    if (base._first === undefined) base._first = u.occurrence_id;
    if (u.occurrence_id === base._first) { failures++; throw new AttachmentTransportError('incomplete', true, 0, 500); }
    return base.putContent(s, u, src, sig);
  } }), { limits: { segment_duration_ms: 1000 } });
  const w = f.manager.startRecording(identity, mono(16000));
  await writeSeconds(f.manager, w, 16000, 3, { flushEvery: 1 });
  await w.finish(10000);
  assert.equal(failures, 3); assert.equal(f.calls.filter(c => c.op === 'abort').length, 1);
  assert.deepEqual(f.requests.map(r => r.segment_index), [0, 0, 1]);
  const m = f.manifests.at(-1).body;
  assert.deepEqual(m.gaps.map(g => [g.start_ms, g.duration_ms, g.reason]), [[0, 1000, 'incomplete']]);
  assert.deepEqual(m.segments.map(s => [s.index, s.start_ms]), [[0, 1000], [1, 2000]]);
  await f.manager.shutdown();
  // Reuse rejected: seg0 ok, seg1 fails, seg2 (reusing index 1) gets 409 -> partial with the published prefix.
  let n = 0;
  const g = await managerV1(base => ({
    putContent: async (s, u, src, sig) => { if (u.occurrence_id === base._second) throw new AttachmentTransportError('incomplete', true, 0, 500); return base.putContent(s, u, src, sig); },
    initialize: async (s, body, key, sig) => {
      n++; const r = JSON.parse(body);
      if (n === 2) base._second = r.occurrence_id;
      if (n === 3) throw new AttachmentTransportError('conflict', false, 0, 409);
      return base.initialize(s, body, key, sig);
    } }), { limits: { segment_duration_ms: 1000 } });
  const w2 = g.manager.startRecording(identity, mono(16000));
  await writeSeconds(g.manager, w2, 16000, 3, { flushEvery: 1 });
  const result = await w2.finish(10000);
  assert.equal(result.state, 'partial');
  const last = g.manifests.at(-1).body;
  assert.deepEqual(last.segments.map(s => s.index), [0]); assert.equal(last.gaps.at(-1).reason, 'incomplete'); assert.equal(last.duration_ms, 3000);
  await g.manager.shutdown();
});
test('T-ST-15 intermediate manifests are prefix-monotonic across 10 PUTs; T-ST-16 latest completed capability only', async () => {
  const f = await managerV1({}, { limits: { segment_duration_ms: 1000 } });
  const w = f.manager.startRecording(identity, mono(8000));
  await writeSeconds(f.manager, w, 8000, 40, { flushEvery: 1 });
  await w.finish(10000);
  assert.ok(f.manifests.length >= 10, `manifests ${f.manifests.length}`);
  for (let i = 1; i < f.manifests.length; i++) {
    const a = f.manifests[i - 1].body; const b = f.manifests[i].body;
    const has = (list, item) => list.some(x => JSON.stringify(x) === JSON.stringify(item));
    assert.ok(a.segments.every(x => has(b.segments, x)) && a.gaps.every(x => has(b.gaps, x)), `manifest ${i} dropped an entry`);
    assert.ok(b.duration_ms >= a.duration_ms);
  }
  assert.deepEqual(f.manifests.map(m => m.key), f.manifests.map((_, i) => `mf-${hex(w.recordingId)}-${i + 1}`));
  for (const m of f.manifests) {
    const top = m.body.segments.at(-1);
    assert.equal(m.capability, f.completed.get(top.occurrence_id), 'manifest must use the latest completed segment capability');
  }
  assert.equal(f.manifests.at(-1).body.capture_complete, true);
  await f.manager.shutdown();
});
test('T-ST-17 capture_complete only with zero gaps (F01 unmarked-capture-gap, complete-without-any-media)', () => {
  const rid = '40000000-0000-4000-8000-000000000001'; const track = '40000000-0000-4000-8000-000000000002';
  const base = { schema_version: 1, recording_id: rid, timebase: 'session_relative_ms', sample_format: 's16le', sample_rate_hz: 16000, channels: 1, codec: 'pcm', container: 'wav',
    layout: 'mixed', tracks: [{ track_id: track, channels: [0], direction: 'mixed', role: 'mixed' }], segments: [], gaps: [{ start_ms: 0, duration_ms: 10, reason: 'incomplete' }], playout: [], capture_complete: true, duration_ms: 10 };
  assert.equal(validAttachmentManifest(base), false); assert.equal(validAttachmentManifest({ ...base, capture_complete: false }), true);
});
test('T-ST-18 tryWrite never performs I/O on the caller (10 000 calls, blocked transport)', async () => {
  const f = await managerV1(() => ({ putContent: (_s, _u, _src, signal) => new Promise((_, reject) => signal.addEventListener('abort', () => reject(new AttachmentTransportError('incomplete')), { once: true })) }));
  const w = f.manager.startRecording(identity, mono(16000));
  const before = f.calls.length; const times = [];
  const chunk = pcm(320);
  for (let i = 0; i < 10000; i++) { const t = process.hrtime.bigint(); w.tryWrite(chunk); times.push(Number(process.hrtime.bigint() - t) / 1e6); }
  assert.equal(f.calls.length, before, 'tryWrite performed transport I/O synchronously');
  times.sort((a, b) => a - b); console.log(`# T-ST-18 p99 tryWrite ms ${times[Math.floor(times.length * 0.99)].toFixed(4)} (recorded, not asserted)`);
  w.abort(); await f.manager.shutdown(100);
});
test('T-ST-19 finish(1000) with a blocked transport returns within 1.25 s', async () => {
  const f = await managerV1(() => ({ initialize: () => new Promise(() => {}) }), { limits: { segment_duration_ms: 1000 } });
  const w = f.manager.startRecording(identity, mono(16000));
  await writeSeconds(f.manager, w, 16000, 2, { flushEvery: 0 });
  const start = Date.now(); const result = await w.finish(1000); const elapsed = Date.now() - start;
  assert.ok(elapsed <= 1250, `finish took ${elapsed} ms`); assert.ok(['partial', 'unpublished'].includes(result.state));
  await f.manager.shutdown(100);
});
test('recording abort is synchronous and PUTs no manifest', async () => {
  const f = await managerV1();
  const w = f.manager.startRecording(identity, mono(16000));
  await writeSeconds(f.manager, w, 16000, 1, { flushEvery: 0 });
  w.abort(); assert.equal(f.manager.status().ownedBytes, 0);
  assert.equal((await w.finish()).state, 'unpublished'); assert.equal(f.manifests.length, 0);
  await f.manager.shutdown();
});

// ---- 10.3 Renewal -------------------------------------------------------------------------------------
function httpDouble(handler) {
  const requests = [];
  const client = async request => {
    const body = request.body === undefined ? undefined : typeof request.body === 'string' ? request.body : Buffer.from(await readAll(request.body));
    const entry = { url: new URL(request.url).pathname, method: request.method, headers: request.headers, body }; requests.push(entry);
    const out = await handler(entry, requests);
    return { status: out.status, body: Buffer.from(out.body === undefined ? '' : typeof out.body === 'string' ? out.body : JSON.stringify(out.body)), retryAfter: out.retryAfter };
  };
  return { requests, client };
}
const sessionBody = (sid, extra = {}) => ({ schema_version: 1, scope: { organization_id: ids[0], workspace_id: ids[1], project_id: ids[2] }, capture_session_id: sid,
  policy: { ...policy, allowed_sources: ['Bytes', 'Blob', 'OwnedRecordingFile', 'ProviderFile', 'RemoteUrl'], record_audio: true }, limits,
  expires_at: new Date(Date.now() + 3600000).toISOString(), transfer_modes: ['authenticated_stream'], ...extra });
let capCounter = 0;
function standardRoutes(entry, overrides = {}) {
  const path = entry.url;
  if (path.endsWith('/attachment-sessions')) return overrides.session ? overrides.session(entry) : { status: 200, body: sessionBody(JSON.parse(entry.body).capture_session_id) };
  if (path.endsWith('/uploads')) {
    if (overrides.upload) { const out = overrides.upload(entry); if (out) return out; }
    const r = JSON.parse(entry.body);
    return { status: 201, body: { upload_id: '50000000-0000-4000-8000-000000000001', attachment_id: r.attachment_id, occurrence_id: r.occurrence_id, state: 'pending_upload',
      reserved_bytes: r.declared_bytes, expires_at: new Date(Date.now() + 600000 + (++capCounter) * 1000).toISOString(), transfer_mode: 'authenticated_stream', write_capability: `synthetic-capability-${capCounter}-abcdef` } };
  }
  if (path.endsWith('/content')) return overrides.content ? overrides.content(entry) : { status: 200, body: { upload_id: path.split('/')[7], state: 'pending_upload', reason: 'queued' } };
  if (path.endsWith('/complete')) return { status: 202, body: { upload_id: path.split('/')[7], state: 'validating', reason: 'binding_pending' } };
  if (path.endsWith('/abort')) return { status: 200, body: { upload_id: path.split('/')[7], state: 'failed', reason: 'incomplete' } };
  if (path.endsWith('/manifest')) return { status: 204 };
  if (path.endsWith('/imports')) { const b = JSON.parse(entry.body); return overrides.imports ? overrides.imports(entry) : { status: 202, body: { mode: 'completed_recording', job_id: '60000000-0000-4000-8000-000000000001', recording_id: b.recording.recording_id, state: 'importing' } }; }
  if (path.includes('/imports/') && path.endsWith('/status')) return { status: 200, body: { job_id: path.split('/')[7], state: 'importing', reason: 'binding_pending' } };
  return { status: 404 };
}
async function httpManager(overrides = {}, managerOptions = {}) {
  const d = httpDouble(entry => standardRoutes(entry, overrides));
  const transport = createAttachmentHttpTransport({ endpoint: 'https://attachments.example', headers: { authorization: 'Bearer synthetic-original', 'x-api-key': 'synthetic-original-key' }, client: d.client });
  const manager = new AttachmentManager({ enabled: true, projectName: 'fixture', projectType: 'observe', environment: () => ({}), transport, recordAudio: true, ...managerOptions });
  await manager.ready();
  return { manager, ...d };
}
test('T-RN-01 session near expiry renews with the same id and nonce under key bs-{sid}-1', async () => {
  const f = await httpManager({ session: entry => ({ status: 200, body: sessionBody(JSON.parse(entry.body).capture_session_id, { expires_at: new Date(Date.now() + 50000).toISOString() }) }) });
  f.manager.preflight(identity, { ...options, maxBytes: 4 }).release();
  for (let i = 0; i < 20 && f.requests.filter(r => r.url.endsWith('/attachment-sessions')).length < 2; i++) await tick();
  const boots = f.requests.filter(r => r.url.endsWith('/attachment-sessions'));
  assert.equal(boots.length, 2);
  assert.equal(boots[0].headers['idempotency-key'], `bs-${hex(f.manager.sessionId)}-0`); assert.equal(boots[1].headers['idempotency-key'], `bs-${hex(f.manager.sessionId)}-1`);
  assert.equal(boots[1].body, boots[0].body); assert.equal(JSON.parse(boots[1].body).correlation_nonce, f.manager.nonce);
  assert.equal(boots[1].headers.authorization, 'Bearer synthetic-original');
  await f.manager.shutdown();
});
test('T-RN-02 renewal with a different scope invalidates the manager (scope_unresolved); T-RN-03 tighter policy drops queued work', async () => {
  let renew;
  const f = await managerV1(base => ({ renewSession: () => new Promise(resolve => { renew = resolve; }) }), { lifetimeMs: 50000 });
  f.manager.preflight(identity, { ...options, maxBytes: 4 }).release(); await tick();
  renew({ ...sessionBody(f.manager.sessionId), scope: { organization_id: ids[0], workspace_id: ids[1], project_id: '70000000-0000-4000-8000-000000000001' } });
  for (let i = 0; i < 10; i++) await tick();
  assert.equal(f.manager.status().state, 'scope_unresolved');
  assert.equal(f.manager.attach(identity, { kind: 'Bytes', data: new Uint8Array(2) }, options).admission, 'scope_unresolved');
  let renew2; let unblock;
  const block = new Promise(resolve => { unblock = resolve; });
  const g = await managerV1(base => ({ renewSession: () => new Promise(resolve => { renew2 = resolve; }), initialize: async (...a) => { await block; return base.initialize(...a); } }), { lifetimeMs: 50000 });
  const handles = [1, 2, 3].map(() => g.manager.attach(identity, { kind: 'Bytes', data: new Uint8Array(2) }, options));
  await tick();
  renew2({ ...sessionBody(g.manager.sessionId), policy: { ...sessionBody(g.manager.sessionId).policy, version: 'v2', allowed_modalities: ['audio'] } });
  for (let i = 0; i < 10; i++) await tick();
  unblock();
  assert.deepEqual((await Promise.all(handles.map(h => h.completion))).map(c => c.status), ['policy_denied', 'policy_denied', 'policy_denied']);
  assert.equal(g.manager.status().ownedBytes, 0);
  assert.equal(g.manager.attach(identity, { kind: 'Bytes', data: new Uint8Array(2) }, { mimeType: 'audio/wav', direction: 'mixed' }).admission, 'queued');
  await f.manager.shutdown(1); await g.manager.shutdown();
});
test('T-RN-04 renewal fails until expiry: state expired, recordings get incomplete gaps, initialized uploads continue', async () => {
  const c = clock(Date.now()); let unblock; const block = new Promise(resolve => { unblock = resolve; });
  const f = await managerV1(base => ({ renewSession: async () => { throw new AttachmentTransportError('incomplete', true); },
    putContent: async (...a) => { await block; return base.putContent(...a); } }), { clock: c, lifetimeMs: 30000 }, { clock: c });
  const h = f.manager.attach(identity, { kind: 'Bytes', data: new Uint8Array(4) }, options);
  const w = f.manager.startRecording(identity, mono(16000));
  assert.equal(w.tryWrite(pcm(16000)), true);
  for (let i = 0; i < 20 && !f.calls.some(x => x.op === 'putContent' || x.op === 'initialize'); i++) await tick();
  c.advance(30000);
  assert.equal(f.manager.status().state, 'expired');
  assert.equal(f.manager.preflight(identity, { ...options, maxBytes: 4 }).reason, 'expired');
  assert.equal(w.tryWrite(pcm(16000)), false);
  unblock(); assert.equal((await h.completion).status, 'validating');
  const result = await w.finish(2000); assert.notEqual(result.state, 'published');
  await f.manager.shutdown(100);
});
test('T-RN-05 capability near expiry before content: replay with the byte-identical body under up-{occ}; same upload_id', async () => {
  let inits = 0;
  const f = await httpManager({ upload: entry => {
    inits++; const r = JSON.parse(entry.body);
    const expires = inits === 1 ? Date.now() + 3000 : Date.now() + 600000;
    return { status: 201, body: { upload_id: '50000000-0000-4000-8000-000000000001', attachment_id: r.attachment_id, occurrence_id: r.occurrence_id, state: 'pending_upload',
      reserved_bytes: r.declared_bytes, expires_at: new Date(expires).toISOString(), transfer_mode: 'authenticated_stream', write_capability: `synthetic-capability-renewed-${inits}` } };
  } });
  const h = f.manager.attach(identity, { kind: 'Bytes', data: new Uint8Array([1, 2, 3]) }, options);
  assert.equal((await h.completion).status, 'validating');
  const uploads = f.requests.filter(r => r.url.endsWith('/uploads'));
  assert.equal(uploads.length, 2); assert.equal(uploads[1].body, uploads[0].body);
  assert.equal(uploads[0].headers['idempotency-key'], `up-${hex(h.occurrenceId)}`); assert.equal(uploads[1].headers['idempotency-key'], uploads[0].headers['idempotency-key']);
  assert.equal(uploads[1].headers.authorization, 'Bearer synthetic-original');
  assert.equal(f.requests.find(r => r.url.endsWith('/content')).headers.authorization, 'Bearer synthetic-capability-renewed-2');
  await f.manager.shutdown();
});
test('T-RN-06 replay returning the same capability: upload expired and aborted, no new upload id', async () => {
  const f = await httpManager({ upload: entry => {
    const r = JSON.parse(entry.body);
    return { status: 201, body: { upload_id: '50000000-0000-4000-8000-000000000001', attachment_id: r.attachment_id, occurrence_id: r.occurrence_id, state: 'pending_upload',
      reserved_bytes: r.declared_bytes, expires_at: new Date(Date.now() + 3000).toISOString(), transfer_mode: 'authenticated_stream', write_capability: 'synthetic-capability-same-0000' } };
  } });
  const h = f.manager.attach(identity, { kind: 'Bytes', data: new Uint8Array([1]) }, options);
  assert.equal((await h.completion).status, 'expired');
  assert.equal(f.requests.filter(r => r.url.endsWith('/abort')).length, 1);
  assert.equal(new Set(f.requests.filter(r => r.url.endsWith('/uploads')).map(r => JSON.parse(r.body).occurrence_id)).size, 1);
  await f.manager.shutdown();
});
test('T-RN-07 401 on content: one renewal then one retry; a second 401 is credential_scope_unsupported', async () => {
  let puts = 0;
  const ok = await httpManager({ content: entry => (++puts === 1 ? { status: 401 } : { status: 200, body: { upload_id: entry.url.split('/')[7], state: 'pending_upload', reason: 'queued' } }) });
  assert.equal((await ok.manager.attach(identity, { kind: 'Bytes', data: new Uint8Array([1]) }, options).completion).status, 'validating');
  assert.equal(ok.requests.filter(r => r.url.endsWith('/uploads')).length, 2);
  const bad = await httpManager({ content: () => ({ status: 401 }) });
  assert.equal((await bad.manager.attach(identity, { kind: 'Bytes', data: new Uint8Array([1]) }, options).completion).status, 'credential_scope_unsupported');
  assert.equal(bad.requests.filter(r => r.url.endsWith('/content')).length, 2);
  await ok.manager.shutdown(); await bad.manager.shutdown();
});
test('T-RN-08 the write capability never appears in status, handles, span attributes or errors', async () => {
  const f = await httpManager({ content: () => ({ status: 500 }) });
  const s = span();
  const h = f.manager.attach(s, { kind: 'Bytes', data: new Uint8Array([1]) }, options);
  const completion = await h.completion;
  const caps = f.requests.filter(r => r.url.endsWith('/content')).map(r => r.headers.authorization.slice(7));
  const haystack = JSON.stringify([f.manager.status(), h, completion, s.attrs, String(new AttachmentTransportError(completion.status))]);
  for (const cap of caps) assert.equal(haystack.includes(cap), false);
  await f.manager.shutdown();
});

// ---- 10.5 Completed import -------------------------------------------------------------------------------
const providerResolved = { source: { kind: 'ProviderFile', connectionId: '80000000-0000-4000-8000-000000000001', registeredOutputId: '80000000-0000-4000-8000-000000000002' },
  declaredBytes: 172800044, declaredDurationMs: 3600000, mimeType: 'audio/wav', direction: 'mixed' };
const resolver = value => ({ calls: 0, async resolve() { this.calls++; return typeof value === 'function' ? value() : value; } });
test('T-IM-01 provider import: RecordingImportRequest body, key im-{rid}, original credentials; T-IM-09 no capability; T-IM-07 one status GET', async () => {
  const f = await httpManager(); const s = span(); const r = resolver(providerResolved);
  const h = await f.manager.importCompletedRecording(s, { provider: 'livekit_egress', providerRef: 'EG_synthetic1' }, { resolver: r });
  assert.equal((await h.completion).status, 'importing'); assert.equal(h.jobId, '60000000-0000-4000-8000-000000000001');
  const post = f.requests.find(x => x.url.endsWith('/imports')); const body = JSON.parse(post.body);
  assert.equal(validAttachmentWire('RecordingImportRequest', body), true);
  assert.equal(post.headers['idempotency-key'], `im-${hex(body.recording.recording_id)}`); assert.equal(post.headers.authorization, 'Bearer synthetic-original');
  assert.equal(body.recording.declared_bytes, 172800044); assert.equal(body.recording.source_kind, 'ProviderFile'); assert.equal(body.recording.deletion_generation, 0);
  assert.equal(s.attrs['fi.attachment.refs'], '[]'); assert.equal(s.attrs['fi.attachment.capture_session_id'], f.manager.sessionId);
  const before = f.requests.length; assert.deepEqual(await h.status(), { state: 'importing', reason: 'binding_pending' }); assert.equal(f.requests.length, before + 1);
  assert.equal(f.requests.at(-1).headers.authorization, 'Bearer synthetic-original');
  // T-IM-08: no URL, path, header or credential in the body.
  assert.equal(/https?:|\/tmp|synthetic-original|credential/.test(post.body), false);
  await f.manager.shutdown();
});
test('T-IM-02 denied preflight never calls the resolver; T-IM-03 timeout or throw -> source_unavailable', async () => {
  const f = await managerV1({}, {}, { traceConfig: { hideOutputs: true } }); const r = resolver(providerResolved);
  assert.equal((await f.manager.importCompletedRecording(identity, { provider: 'livekit_egress', providerRef: 'EG_1' }, { resolver: r })).reason, 'policy_denied'); assert.equal(r.calls, 0);
  const g = await managerV1();
  const slow = { resolve: () => new Promise(() => {}) };
  const start = Date.now(); const h = await g.manager.importCompletedRecording(identity, { provider: 'livekit_egress', providerRef: 'EG_1' }, { resolver: slow, timeoutMs: 200 });
  assert.equal(h.reason, 'source_unavailable'); assert.ok(Date.now() - start < 1000);
  const thrower = { resolve: async () => { throw Error('synthetic-secret-detail'); } };
  const t = await g.manager.importCompletedRecording(identity, { provider: 'livekit_egress', providerRef: 'EG_1' }, { resolver: thrower });
  assert.equal(t.reason, 'source_unavailable'); assert.equal(JSON.stringify(t).includes('synthetic-secret-detail'), false);
  await f.manager.shutdown(1); await g.manager.shutdown(1);
});
test('T-IM-04 out-of-range size or duration -> too_large before submit; T-IM-05 narrowing mixed -> policy_denied; T-IM-06 envelope', async () => {
  const f = await managerV1();
  for (const bad of [{ declaredBytes: 1073741825 }, { declaredDurationMs: 7200001 }]) {
    assert.equal((await f.manager.importCompletedRecording(identity, { provider: 'livekit_egress', providerRef: 'EG_1' }, { resolver: resolver({ ...providerResolved, ...bad }) })).reason, 'too_large');
  }
  assert.equal((await f.manager.importCompletedRecording(identity, { provider: 'livekit_egress', providerRef: 'EG_1', direction: 'mixed' }, { resolver: resolver({ ...providerResolved, direction: 'output' }) })).reason, 'policy_denied');
  const widened = await f.manager.importCompletedRecording(identity, { provider: 'livekit_egress', providerRef: 'EG_1', direction: 'output' }, { resolver: resolver({ ...providerResolved, direction: 'mixed' }) });
  assert.equal((await widened.completion).status, 'importing'); assert.equal(f.imports.at(-1).body.recording.direction, 'mixed');
  const envelope = await f.manager.importCompletedRecording(identity, { provider: 'trusted_importer', providerRef: 'ENV_1' }, { resolver: resolver({ ...providerResolved, source: { kind: 'RemoteUrlEnvelope', envelopeId: '80000000-0000-4000-8000-000000000003' } }) });
  assert.equal((await envelope.completion).status, 'importing');
  assert.deepEqual(f.imports.at(-1).body.source, { kind: 'RemoteUrlEnvelope', envelope_id: '80000000-0000-4000-8000-000000000003' }); assert.equal(f.imports.at(-1).body.recording.source_kind, 'RemoteUrl');
  assert.equal(f.calls.filter(c => c.op === 'submitImport').length, 2);
  await f.manager.shutdown();
});
test('T-IM-10 429 with Retry-After 1 three times -> throttled within the deadline', async () => {
  const f = await httpManager({ imports: () => ({ status: 429, retryAfter: '1' }) });
  const start = Date.now();
  const h = await f.manager.importCompletedRecording(identity, { provider: 'livekit_egress', providerRef: 'EG_1' }, { resolver: resolver(providerResolved) });
  assert.equal((await h.completion).status, 'throttled'); assert.equal(f.requests.filter(r => r.url.endsWith('/imports')).length, 3);
  assert.ok(Date.now() - start < 5000);
  await f.manager.shutdown();
});

// ---- 10.6 Consumed streams -------------------------------------------------------------------------------
test('T-CS-01 app-owned reader: chunks observed after the app reads; no extra reads, no clone or tee', async () => {
  const f = await managerV1(); const data = [new Uint8Array([1, 2]), new Uint8Array([3, 4, 5])];
  let pulls = 0;
  const body = new ReadableStream({ pull(controller) { pulls++; const next = data.shift(); if (next) controller.enqueue(next); else controller.close(); } });
  const stream = f.manager.startAttachmentStream(identity, { mimeType: 'audio/mpeg', direction: 'output', maxBytes: 64 });
  const reader = body.getReader(); // the APPLICATION obtained the reader
  const read = reader.read.bind(reader); let appReads = 0;
  reader.read = async () => { appReads++; const r = await read(); if (!r.done) stream.tryWrite(r.value); else stream.finish(); return r; };
  while (!(await reader.read()).done) { /* app consumes */ }
  assert.ok(pulls <= appReads + 1);
  const h = stream.finish(); assert.equal((await h.completion).status, 'validating');
  assert.deepEqual([...f.contents.get(h.occurrenceId)], [1, 2, 3, 4, 5]);
  await f.manager.shutdown();
});
test('T-CS-02 over the cap: all discarded, too_large, nothing uploaded; T-CS-03 app cancel -> incomplete, budget released', async () => {
  const f = await managerV1(); const s = span();
  const stream = f.manager.startAttachmentStream(s, { mimeType: 'audio/mpeg', direction: 'output', maxBytes: 4 });
  assert.equal(stream.tryWrite(new Uint8Array(3)), true); assert.equal(stream.tryWrite(new Uint8Array(3)), false);
  assert.equal(stream.reason, 'too_large'); assert.equal((await stream.finish().completion).status, 'too_large');
  assert.equal(s.attrs['fi.attachment.refs'], undefined, 'an over-cap stream projects nothing');
  const other = f.manager.startAttachmentStream(identity, { mimeType: 'audio/mpeg', direction: 'output', maxBytes: 64 });
  other.tryWrite(new Uint8Array(8)); other.abort();
  assert.equal(other.reason, 'incomplete'); assert.equal(f.manager.status().ownedBytes, 0); assert.equal(f.manager.status().openStreams, 0);
  await f.manager.flush(); assert.equal(f.ops().includes('initialize'), false);
  await f.manager.shutdown();
});
test('T-CS-04 abandoned stream aborted at the next entry point after idle TTL (fake clock, no timer)', async () => {
  const c = clock(); const f = await managerV1({}, { clock: c });
  const stream = f.manager.startAttachmentStream(identity, { mimeType: 'audio/mpeg', direction: 'output', maxBytes: 64, idleTtlMs: 1000 });
  stream.tryWrite(new Uint8Array(4)); c.advance(1001);
  assert.equal(f.manager.status().openStreams, 0); assert.equal(stream.reason, 'incomplete'); assert.equal(f.manager.status().ownedBytes, 0);
  await f.manager.shutdown(1);
});
test('T-CS-06 BYOB buffer reused after tryWrite: uploaded bytes equal the bytes at write time', async () => {
  const f = await managerV1();
  const stream = f.manager.startAttachmentStream(identity, { mimeType: 'audio/mpeg', direction: 'output', maxBytes: 64 });
  const buffer = new Uint8Array([9, 9, 9]); stream.tryWrite(buffer); buffer.fill(0); stream.tryWrite(buffer.subarray(0, 1));
  const h = stream.finish(); assert.equal((await h.completion).status, 'validating');
  assert.deepEqual([...f.contents.get(h.occurrenceId)], [9, 9, 9, 0]);
  await f.manager.shutdown();
});

// ---- 10.7 Channel and role -----------------------------------------------------------------------------------
test('T-CH-01 UploadRequest never carries role or channel; T-CH-02 the live reference does', async () => {
  const f = await managerV1(); const s = span();
  const h = f.manager.attach(s, { kind: 'Bytes', data: new Uint8Array([1]) }, { mimeType: 'audio/wav', direction: 'output', role: 'assistant', channel: 1, messageIndex: 2 });
  await h.completion;
  assert.equal('role' in f.requests[0] || 'channel' in f.requests[0], false); assert.equal(validAttachmentWire('UploadRequest', f.requests[0]), true);
  const refs = JSON.parse(s.attrs['fi.attachment.refs']); assert.equal(refs[0].role, 'assistant'); assert.equal(refs[0].channel, 1);
  assert.equal(validAttachmentWire('SpanAttributes', s.attrs), true);
  await f.manager.shutdown();
});
test('T-CH-03 span ended before attach: no projection, the upload still proceeds; T-CH-05 carrier stamp only on admitted targets', async () => {
  const f = await managerV1(); const ended = span(); ended.end();
  const h = f.manager.attach(ended, { kind: 'Bytes', data: new Uint8Array([1]) }, options);
  assert.equal((await h.completion).status, 'validating'); assert.equal(ended.writes, 0);
  const bystander = span(); f.manager.correlation(bystander); assert.equal(bystander.writes, 0, 'non-target spans are never stamped');
  const denied = span(); f.manager.attach(denied, { kind: 'Bytes', data: new Uint8Array([1]) }, { ...options, capture: { captureContent: false } });
  assert.equal(denied.writes, 0, 'a denied capture stamps nothing');
  const aborted = span(); const st = f.manager.startAttachmentStream(aborted, { mimeType: 'audio/mpeg', direction: 'output', maxBytes: 8 }); st.tryWrite(new Uint8Array(1)); st.abort();
  assert.equal(aborted.writes, 0, 'an aborted stream projects nothing');
  for (const make of [s => f.manager.attach(s, { kind: 'Bytes', data: new Uint8Array([1]) }, options),
    s => { const x = f.manager.startAttachmentStream(s, { mimeType: 'audio/mpeg', direction: 'output', maxBytes: 8 }); x.tryWrite(new Uint8Array(1)); x.finish(); },
    s => f.manager.startRecording(s, mono(16000)).abort()]) {
    const target = span(); make(target);
    assert.equal(validAttachmentWire('SpanAttributes', target.attrs), true, JSON.stringify(target.attrs));
  }
  await f.manager.flush(); await f.manager.shutdown();
});
test('T-CH-06 explicit workspace with a different response scope -> scope_unresolved', async () => {
  const f = await managerV1({}, { ready: false }, { workspaceId: '90000000-0000-4000-8000-000000000001' });
  assert.equal(await f.manager.ready(), false); assert.equal(f.manager.status().state, 'scope_unresolved');
  assert.equal(f.manager.attach(identity, source(), { mimeType: 'audio/wav', direction: 'mixed' }).admission, 'scope_unresolved');
  await f.manager.shutdown(1);
});

// ---- 10.8 Lifecycle ----------------------------------------------------------------------------------------
test('T-LC-02/03/04/05 disabled null object: no timers, sockets, listeners, finalizers or span writes; every call immediate', async () => {
  const counts = { timers: 0, intervals: 0, registry: 0 };
  const saved = { st: global.setTimeout, si: global.setInterval, fr: global.FinalizationRegistry };
  global.setTimeout = (...a) => { counts.timers++; return saved.st(...a); };
  global.setInterval = (...a) => { counts.intervals++; return saved.si(...a); };
  global.FinalizationRegistry = class extends saved.fr { constructor(...a) { counts.registry++; super(...a); } };
  const listenersBefore = process.eventNames().map(n => process.listenerCount(n)).reduce((a, b) => a + b, 0);
  try {
    const { createNodeAttachmentManager } = require('./attachments-v1.cjs').nodeCore;
    const m = createNodeAttachmentManager({ environment: () => ({}) });
    assert.equal(m.constructor.name, 'DisabledAttachmentManager');
    const s = span(); const start = process.hrtime.bigint();
    assert.equal(await m.ready(), false); assert.equal(m.status().state, 'disabled');
    let touched = false;
    const handle = m.attach(s, { kind: 'Bytes', get data() { touched = true; return new Uint8Array(1); } }, options);
    assert.equal(handle.admission, 'disabled'); assert.deepEqual(await handle.completion, { status: 'disabled' });
    assert.equal(m.preflight(s, { ...options, maxBytes: 1 }).reason, 'disabled');
    const w = m.startRecording(s, mono(16000)); assert.equal(w.tryWrite(pcm(10)), false); assert.equal((await w.finish()).state, 'denied');
    const st = m.startAttachmentStream(s, { mimeType: 'audio/mpeg', direction: 'output', maxBytes: 8 }); assert.equal(st.tryWrite(new Uint8Array(1)), false);
    let resolved = false;
    assert.equal((await m.importCompletedRecording(s, { provider: 'x', providerRef: 'y' }, { resolver: { resolve: async () => { resolved = true; return 'source_unavailable'; } } })).reason, 'disabled');
    assert.equal(m.attachOwnedRecording(s, { kind: 'OwnedRecordingFile', lease: {} }, { mimeType: 'audio/wav', direction: 'mixed' }).reason, 'disabled');
    m.updatePolicy({ enabled: true }); assert.equal(m.status().state, 'disabled');
    assert.deepEqual(await m.flush(), { unfinished: 0, dropped: 0 }); assert.deepEqual(await m.shutdown(), { unfinished: 0, dropped: 0 });
    const elapsed = Number(process.hrtime.bigint() - start) / 1e6;
    assert.equal(touched, false); assert.equal(resolved, false); assert.equal(s.writes, 0);
    assert.ok(elapsed < 50, `disabled calls took ${elapsed} ms`);
  } finally { global.setTimeout = saved.st; global.setInterval = saved.si; global.FinalizationRegistry = saved.fr; }
  const listenersAfter = process.eventNames().map(n => process.listenerCount(n)).reduce((a, b) => a + b, 0);
  assert.deepEqual(counts, { timers: 0, intervals: 0, registry: 0 }); assert.equal(listenersAfter, listenersBefore);
});
test('T-LC-12 status().state is "ready" after bootstrap', async () => {
  const f = await managerV1(); assert.equal(f.manager.status().state, 'ready'); await f.manager.shutdown();
});
test('T-LC-15 shutdown is idempotent and concurrent; T-LC-16 returns within timeout + 250 ms with a transport that ignores abort', async () => {
  const f = await managerV1(() => ({ putContent: () => new Promise(() => {}) }));
  f.manager.attach(identity, { kind: 'Bytes', data: new Uint8Array(4) }, options);
  await tick(); await tick();
  const start = Date.now(); const a = f.manager.shutdown(500); const b = f.manager.shutdown(500);
  assert.equal(a, b); const result = await a; const elapsed = Date.now() - start;
  assert.ok(elapsed <= 750, `shutdown took ${elapsed} ms`); assert.ok(result.unfinished >= 1);
});

// ---- 10.9 Transport and wire -------------------------------------------------------------------------------
test('T-TR-02 status -> reason table, Error.code refinement only within class', () => {
  const body = code => Buffer.from(JSON.stringify({ code, request_id: '10000000-0000-4000-8000-000000000009' }));
  const rows = [[400, 'unsupported_version', false], [401, 'credential_scope_unsupported', false], [403, 'policy_denied', false], [404, 'not_found', false],
    [409, 'conflict', false], [410, 'expired', false], [413, 'too_large', false], [429, 'throttled', true], [408, 'incomplete', true], [500, 'incomplete', true],
    [502, 'incomplete', true], [503, 'incomplete', true], [504, 'incomplete', true], [418, 'incomplete', false], [302, 'incomplete', false]];
  for (const [status, code, retryable] of rows) { const e = attachmentHttpError(status); assert.equal(e.code, code, String(status)); assert.equal(e.retryable, retryable, String(status)); }
  assert.equal(attachmentHttpError(409, body('deleted')).code, 'deleted'); assert.equal(attachmentHttpError(403, body('redaction_unavailable')).code, 'redaction_unavailable');
  assert.equal(attachmentHttpError(401, body('expired')).code, 'expired'); assert.equal(attachmentHttpError(404, body('scope_unresolved')).code, 'scope_unresolved');
  assert.equal(attachmentHttpError(403, body('deleted')).code, 'policy_denied', 'out-of-class code ignored');
  assert.equal(attachmentHttpError(500, body('deleted')).code, 'incomplete');
  assert.equal(attachmentHttpError(409, Buffer.from('{"code":"deleted","code":"conflict","request_id":"10000000-0000-4000-8000-000000000009"}')).code, 'conflict', 'duplicate members ignored');
  assert.equal(attachmentHttpError(429, undefined, '3').retryAfterMs, 3000); assert.equal(attachmentHttpError(429, undefined, '60').retryAfterMs, 5000);
  assert.equal(attachmentHttpError(429, undefined, 'Wed, 21 Oct 2015 07:28:00 GMT').retryAfterMs, 1000); assert.equal(attachmentHttpError(429).retryAfterMs, 1000);
});
test('T-TR-03 idempotency keys on every route and byte-identical replays', async () => {
  let inits = 0;
  const f = await httpManager({ upload: entry => (++inits === 1 ? { status: 503 } : undefined), content: () => ({ status: 409 }) });
  const h = f.manager.attach(identity, { kind: 'Bytes', data: new Uint8Array([1]) }, options);
  assert.equal((await h.completion).status, 'conflict');
  const uploads = f.requests.filter(r => r.url.endsWith('/uploads'));
  assert.equal(uploads.length, 2); assert.equal(uploads[0].body, uploads[1].body); assert.equal(uploads[0].headers['idempotency-key'], `up-${hex(h.occurrenceId)}`);
  const abort = f.requests.find(r => r.url.endsWith('/abort'));
  assert.equal(abort.headers['idempotency-key'], `ab-${hex('50000000-0000-4000-8000-000000000001')}`); assert.match(abort.headers.authorization, /^Bearer synthetic-capability-/);
  assert.equal(attachmentIdempotencyKey('complete', '50000000-0000-4000-8000-000000000001'), 'cp-50000000000040008000000000000001');
  const g = await httpManager({}, {});
  await g.manager.attach(identity, { kind: 'Bytes', data: new Uint8Array([1]) }, options).completion;
  const complete = g.requests.find(r => r.url.endsWith('/complete'));
  assert.equal(complete.headers['idempotency-key'], `cp-${hex('50000000-0000-4000-8000-000000000001')}`); assert.deepEqual(JSON.parse(complete.body), { policy_version: 'v1', deletion_generation: 0 });
  const w = g.manager.startRecording(identity, mono(8000)); w.tryWrite(pcm(8000)); await w.finish(5000);
  const manifest = g.requests.find(r => r.url.endsWith('/manifest'));
  assert.equal(manifest.method, 'PUT'); assert.equal(manifest.headers['idempotency-key'], `mf-${hex(w.recordingId)}-1`); assert.match(manifest.headers.authorization, /^Bearer synthetic-capability-/);
  assert.equal(manifest.headers['x-api-key'], undefined, 'capability routes never carry original credentials');
  await f.manager.shutdown(); await g.manager.shutdown();
});
test('T-TR-05 content PUT pacing honours upload_bytes_per_second', async () => {
  const d = httpDouble(entry => standardRoutes(entry, { session: e => ({ status: 200, body: sessionBody(JSON.parse(e.body).capture_session_id, { limits: { ...limits, upload_bytes_per_second: 400000 } }) }) }));
  const transport = createAttachmentHttpTransportV1({ endpoint: 'https://attachments.example', headers: { authorization: 'Bearer synthetic-original' }, client: async request => {
    if (request.pacer && request.body && typeof request.body !== 'string') { const size = request.body.byteLength ?? request.body.size; for (let o = 0; o < size; o += 262144) await request.pacer.take(Math.min(262144, size - o), request.signal); }
    return d.client(request);
  } });
  const m = new AttachmentManager({ enabled: true, projectName: 'fixture', projectType: 'observe', environment: () => ({}), transport });
  await m.ready(); const start = Date.now();
  assert.equal((await m.attach(identity, { kind: 'Bytes', data: new Uint8Array(800000) }, { mimeType: 'audio/mpeg', direction: 'output' }).completion).status, 'validating');
  const elapsed = Date.now() - start; assert.ok(elapsed >= 700, `paced upload took ${elapsed} ms`);
  const put = d.requests.find(r => r.url.endsWith('/content')); assert.equal(put.body.length, 800000);
  await m.shutdown();
});
test('T-TR-06 oversized or duplicate-member responses -> unsupported_version; T-TR-07 missing capability rejected; T-TR-08 validating, never ready', async () => {
  const big = httpDouble(entry => (entry.url.endsWith('/attachment-sessions') ? { status: 200, body: 'x'.repeat(70000) } : { status: 404 }));
  const t1 = createAttachmentHttpTransportV1({ endpoint: 'https://attachments.example', headers: {}, client: big.client });
  await assert.rejects(t1.bootstrap({ capture_session_id: ids[0] }, 'bs-x-0', new AbortController().signal), e => e.code === 'unsupported_version');
  const dup = httpDouble(() => ({ status: 200, body: '{"schema_version":1,"schema_version":1}' }));
  const t2 = createAttachmentHttpTransportV1({ endpoint: 'https://attachments.example', headers: {}, client: dup.client });
  await assert.rejects(t2.bootstrap({ capture_session_id: ids[0] }, 'bs-x-0', new AbortController().signal), e => e.code === 'unsupported_version');
  const f = await httpManager({ upload: entry => { const r = JSON.parse(entry.body); return { status: 201, body: { upload_id: '50000000-0000-4000-8000-000000000001', attachment_id: r.attachment_id,
    occurrence_id: r.occurrence_id, state: 'pending_upload', reserved_bytes: r.declared_bytes, expires_at: new Date(Date.now() + 600000).toISOString(), transfer_mode: 'authenticated_stream' } }; } });
  assert.equal((await f.manager.attach(identity, { kind: 'Bytes', data: new Uint8Array([1]) }, options).completion).status, 'unsupported_version');
  assert.equal(f.requests.some(r => r.url.endsWith('/content')), false);
  const g = await httpManager();
  assert.equal((await g.manager.attach(identity, { kind: 'Bytes', data: new Uint8Array([1]) }, options).completion).status, 'validating');
  await f.manager.shutdown(); await g.manager.shutdown();
});
test('legacy four-method transports keep working through the adapter (stacked lanes)', async () => {
  const { fake } = require('./attachments-fixtures.cjs'); const f = fake();
  const m = new AttachmentManager({ enabled: true, projectName: 'fixture', projectType: 'observe', environment: () => ({}), transport: f.transport });
  assert.equal(await m.ready(), true); assert.equal(m.status().state, 'ready');
  assert.equal((await m.attach(identity, source(), { mimeType: 'audio/wav', direction: 'mixed' }).completion).status, 'validating');
  assert.deepEqual(f.calls, ['bootstrap', 'initialize', 'upload', 'complete']);
  const w = m.startRecording(identity, mono(8000)); w.tryWrite(pcm(800)); const r = await w.finish(2000);
  assert.equal(r.state, 'unpublished', 'legacy transports cannot PUT manifests: fail closed');
  await m.shutdown();
});
