/* Owned recording files (design section 4, T-LS-*). Synthetic files under a private temp root only. */
const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { nodeCore, core, managerV1, readAll } = require('./attachments-v1.cjs');
const { identity } = require('./attachments-fixtures.cjs');
const { createRecordingFileScope } = nodeCore;
const { attachmentWavHeader, validAttachmentManifest, AttachmentTransportError } = core;
const live = setInterval(() => {}, 1000);
after(() => clearInterval(live));
const MiB = 1024 * 1024;
function root() { return fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'owned-rec-'))); }
function pattern(size, seed = 1) { const b = Buffer.alloc(size); for (let i = 0; i < size; i++) b[i] = (i * 13 + seed + (i >> 11)) & 0xff; return b; }
function wavFile(file, { rate = 24000, channels = 2, frames, bits = 16, format = 1, badLength = false }) {
  const data = pattern(frames * channels * (bits / 8), 7);
  const header = Buffer.from(attachmentWavHeader(channels, rate, badLength ? data.length + 100 : data.length));
  header.writeUInt16LE(format, 20); header.writeUInt16LE(bits, 34); header.writeUInt16LE(channels * bits / 8, 32);
  fs.writeFileSync(file, Buffer.concat([header, data]));
  return data;
}
const stereo = [{ channels: [0], direction: 'input', role: 'user' }, { channels: [1], direction: 'output', role: 'assistant' }];

test('T-LS-02 symlink leaf and T-LS-03 symlinked intermediate are source_unavailable; foreign refs fail closed', async () => {
  const dir = root(); fs.writeFileSync(path.join(dir, 'real.ogg'), pattern(100));
  fs.symlinkSync(path.join(dir, 'real.ogg'), path.join(dir, 'link.ogg'));
  fs.mkdirSync(path.join(dir, 'real-dir')); fs.writeFileSync(path.join(dir, 'real-dir', 'a.ogg'), pattern(100)); fs.symlinkSync(path.join(dir, 'real-dir'), path.join(dir, 'dir-link'));
  const scope = await createRecordingFileScope(dir, { enabled: true });
  await assert.rejects(scope.identify('link.ogg'), e => e.code === 'source_unavailable');
  await assert.rejects(scope.identify('dir-link/a.ogg'), e => e.code === 'source_unavailable');
  await assert.rejects(scope.identify('../etc/passwd'), e => e.code === 'source_unavailable');
  await assert.rejects(createRecordingFileScope(path.join(dir, 'dir-link'), { enabled: true }), e => e.code === 'source_unavailable');
  const f = await managerV1();
  const h = f.manager.attachOwnedRecording(identity, { kind: 'OwnedRecordingFile', lease: {} }, { mimeType: 'audio/ogg', direction: 'mixed' });
  assert.equal(h.reason, 'source_unavailable'); assert.equal(h.tier, 'denied');
  await scope.close(); await f.manager.shutdown();
});
test('T-LS-06 12 MiB OGG: tier single, streamed from the lease, owned memory never above one slice; T-CH-04 mixed, no channel', async () => {
  const dir = root(); const data = pattern(12 * MiB); fs.writeFileSync(path.join(dir, 'call.ogg'), data);
  let peak = 0; let manager;
  const f = await managerV1(base => ({ putContent: async (s, u, src, sig) => {
    const out = Buffer.alloc(src.size);
    for (let o = 0; o < src.size; o += 262144) { await src.check(); const p = await src.read(o, Math.min(262144, src.size - o)); Buffer.from(p).copy(out, o); peak = Math.max(peak, manager.status().ownedBytes); }
    f.contents.set(u.occurrence_id, out);
  } }));
  manager = f.manager;
  const scope = await createRecordingFileScope(dir, { enabled: true });
  const id = await scope.identify('call.ogg'); assert.equal(id.size, 12 * MiB);
  const h = f.manager.attachOwnedRecording(identity, scope.source('call.ogg', id), { mimeType: 'audio/ogg', direction: 'mixed' });
  assert.equal(h.tier, 'single'); assert.equal((await h.wait(30000)).status, 'validating');
  assert.ok(peak <= 262144, `owned peak ${peak}`);
  assert.ok(Buffer.from(f.contents.values().next().value).equals(data));
  const request = f.requests[0];
  assert.equal(request.direction, 'mixed'); assert.equal(request.source_kind, 'OwnedRecordingFile'); assert.equal('channel' in request || 'role' in request, false);
  assert.equal(request.recording_id, undefined);
  await scope.close(); await f.manager.shutdown();
});
test('T-LS-07 70 MiB OGG: too_large with zero bytes read', async () => {
  const dir = root(); const file = path.join(dir, 'big.ogg'); const fd = fs.openSync(file, 'w'); fs.ftruncateSync(fd, 70 * MiB); fs.closeSync(fd);
  const f = await managerV1();
  const scope = await createRecordingFileScope(dir, { enabled: true });
  const h = f.manager.attachOwnedRecording(identity, scope.source('big.ogg', await scope.identify('big.ogg')), { mimeType: 'audio/ogg', direction: 'mixed' });
  assert.equal(h.tier, 'denied'); assert.equal(h.reason, 'too_large'); await f.manager.flush();
  assert.equal(f.ops().includes('initialize'), false);
  await scope.close(); await f.manager.shutdown();
});
test('T-LS-08 90 MiB 24 kHz stereo WAV with wavTracks: segmented from lease ranges, stereo manifest, bytes identical', async () => {
  const dir = root(); const frames = Math.floor(90 * MiB / 4);
  const data = wavFile(path.join(dir, 'call.wav'), { rate: 24000, channels: 2, frames });
  const f = await managerV1();
  const scope = await createRecordingFileScope(dir, { enabled: true });
  const h = f.manager.attachOwnedRecording(identity, scope.source('call.wav', await scope.identify('call.wav')), { mimeType: 'audio/wav', direction: 'mixed', wavTracks: stereo });
  assert.equal(h.tier, 'wav_recording');
  assert.equal((await h.wait(30000)).status, 'validating'); assert.ok(h.recordingId);
  const m = f.manifests.at(-1).body;
  assert.equal(m.layout, 'stereo'); assert.deepEqual(m.tracks.map(t => [t.channels, t.role]), [[[0], 'user'], [[1], 'assistant']]);
  assert.equal(m.capture_complete, true); assert.equal(m.duration_ms, Math.ceil(frames * 1000 / 24000)); assert.equal(validAttachmentManifest(m), true);
  const joined = Buffer.concat(m.segments.map(s => f.contents.get(s.occurrence_id).subarray(44)));
  assert.ok(joined.equals(data), 'decoded segment frames must equal the source frames');
  assert.ok(f.requests.every(r => r.declared_bytes <= 8 * 1024 * 1024 && r.recording_id === h.recordingId));
  await scope.close(); await f.manager.shutdown();
});
test('T-LS-09 bad data length, non-PCM and 24-bit WAV: single when within the cap, too_large above it', async () => {
  const dir = root();
  wavFile(path.join(dir, 'bad.wav'), { frames: 1000, badLength: true });
  wavFile(path.join(dir, 'float.wav'), { frames: 1000, format: 3 });
  wavFile(path.join(dir, 'b24.wav'), { frames: 1000, bits: 24 });
  const f = await managerV1();
  const scope = await createRecordingFileScope(dir, { enabled: true });
  for (const name of ['bad.wav', 'float.wav', 'b24.wav']) {
    const h = f.manager.attachOwnedRecording(identity, scope.source(name, await scope.identify(name)), { mimeType: 'audio/wav', direction: 'mixed', wavTracks: stereo });
    assert.equal((await h.wait(10000)).status, 'validating', name); assert.equal(h.tier, 'single', name);
  }
  const big = path.join(dir, 'big.wav'); fs.writeFileSync(big, Buffer.from(attachmentWavHeader(2, 24000, 100))); const fd = fs.openSync(big, 'r+'); fs.ftruncateSync(fd, 60 * MiB); fs.closeSync(fd);
  const h = f.manager.attachOwnedRecording(identity, scope.source('big.wav', await scope.identify('big.wav')), { mimeType: 'audio/wav', direction: 'mixed', wavTracks: stereo });
  assert.equal((await h.wait(10000)).status, 'too_large'); assert.equal(h.tier, 'denied');
  await scope.close(); await f.manager.shutdown();
});
test('T-LS-01 file changed during transfer -> source_unavailable and abort; T-LS-04 the app file position is unchanged', async () => {
  const dir = root(); const file = path.join(dir, 'grow.ogg'); fs.writeFileSync(file, pattern(MiB));
  const appFd = fs.openSync(file, 'r'); const probe = Buffer.alloc(10); fs.readSync(appFd, probe, 0, 10, null);
  const f = await managerV1(base => ({ putContent: async (s, u, src) => {
    for (let o = 0; o < src.size; o += 262144) { await src.check(); await src.read(o, 262144); if (o === 0) fs.appendFileSync(file, 'x'); }
    await src.check();
  } }));
  const scope = await createRecordingFileScope(dir, { enabled: true });
  const h = f.manager.attachOwnedRecording(identity, scope.source('grow.ogg', await scope.identify('grow.ogg')), { mimeType: 'audio/ogg', direction: 'mixed' });
  assert.equal((await h.wait(10000)).status, 'source_unavailable');
  assert.equal(f.ops().filter(op => op === 'abort').length, 1);
  const next = Buffer.alloc(1); fs.readSync(appFd, next, 0, 1, null); assert.equal(next[0], pattern(11)[10]);
  fs.closeSync(appFd); await scope.close(); await f.manager.shutdown();
});
test('T-LS-05 intermediate directory swapped to a symlink after identify: identity bind fails closed (documented residual race)', async () => {
  const dir = root(); fs.mkdirSync(path.join(dir, 'session')); fs.writeFileSync(path.join(dir, 'session', 'r.ogg'), pattern(1000));
  fs.mkdirSync(path.join(dir, 'other')); fs.writeFileSync(path.join(dir, 'other', 'r.ogg'), pattern(1000, 9));
  const scope = await createRecordingFileScope(dir, { enabled: true });
  const id = await scope.identify('session/r.ogg'); const src = scope.source('session/r.ogg', id);
  fs.renameSync(path.join(dir, 'session'), path.join(dir, 'session-real')); fs.symlinkSync(path.join(dir, 'other'), path.join(dir, 'session'));
  const f = await managerV1();
  const h = f.manager.attachOwnedRecording(identity, src, { mimeType: 'audio/ogg', direction: 'mixed' });
  assert.equal((await h.wait(10000)).status, 'source_unavailable'); assert.equal(f.ops().includes('initialize'), false);
  // Same inode reached through a symlinked directory is still refused (lstat of every component).
  fs.unlinkSync(path.join(dir, 'session')); fs.symlinkSync(path.join(dir, 'session-real'), path.join(dir, 'session'));
  const h2 = f.manager.attachOwnedRecording(identity, src, { mimeType: 'audio/ogg', direction: 'mixed' });
  assert.equal((await h2.wait(10000)).status, 'source_unavailable');
  await scope.close(); await f.manager.shutdown();
});
test('T-LS-10 finalization deadline with a blocked transport: incomplete within 1.25 s; lease and scope closed', async () => {
  const dir = root(); fs.writeFileSync(path.join(dir, 'r.ogg'), pattern(600000));
  const f = await managerV1(() => ({ putContent: (_s, _u, _src, signal) => new Promise((_, reject) => signal.addEventListener('abort', () => reject(new AttachmentTransportError('incomplete')), { once: true })) }));
  const scope = await createRecordingFileScope(dir, { enabled: true });
  const h = f.manager.attachOwnedRecording(identity, scope.source('r.ogg', await scope.identify('r.ogg')), { mimeType: 'audio/ogg', direction: 'mixed' });
  const start = Date.now(); const result = await h.wait(1000); const elapsed = Date.now() - start;
  assert.equal(result.status, 'incomplete'); assert.ok(elapsed <= 1250, `wait took ${elapsed} ms`);
  const closing = Date.now(); await scope.close(); assert.ok(Date.now() - closing < 500);
  await f.manager.shutdown(500); assert.equal(f.manager.status().ownedBytes, 0);
});
