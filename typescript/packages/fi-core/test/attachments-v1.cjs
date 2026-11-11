// Synthetic doubles for the versioned wire surface (design section 9). No network, no real credentials.
const path = require('node:path');
const { createRequire } = require('node:module');
const packageJson = process.env.FI_CORE_PACKAGE_JSON || path.resolve(__dirname, '../package.json');
const load = createRequire(packageJson);
const core = load('@traceai/fi-core/attachments');
const nodeCore = load('@traceai/fi-core/attachments/node');
const { ids, policy, limits, identity } = require('./attachments-fixtures.cjs');

async function readAll(source) {
  if (source instanceof Uint8Array) return Buffer.from(source);
  if (typeof Blob !== 'undefined' && source instanceof Blob) return Buffer.from(await source.arrayBuffer());
  const out = Buffer.alloc(source.size);
  for (let offset = 0; offset < source.size; offset += 262144) {
    await source.check();
    const part = await source.read(offset, Math.min(262144, source.size - offset));
    Buffer.from(part.buffer, part.byteOffset, part.byteLength).copy(out, offset);
  }
  await source.check();
  return out;
}
function clock(start = 1_700_000_000_000) {
  let t = 0;
  return { now: () => t, wall: () => start + t, advance(ms) { t += ms; }, set(ms) { t = ms; } };
}
/** A complete in-memory AttachmentTransportV1. `overrides(base)` may replace any method. */
function v1(overrides = {}, options = {}) {
  const calls = []; let n = 0;
  const id = () => `30000000-0000-4000-8000-${(++n).toString(16).padStart(12, '0')}`;
  const cap = () => `synthetic-capability-${(++n).toString(36)}-abcdefgh`;
  const wall = options.clock ? options.clock.wall : () => Date.now();
  const contents = new Map(); const manifests = []; const imports = []; const requests = []; const completed = new Map();
  const session = sid => ({ schema_version: 1, scope: { organization_id: ids[0], workspace_id: ids[1], project_id: ids[2] }, capture_session_id: sid,
    policy: { ...policy, allowed_sources: ['Bytes', 'Blob', 'OwnedRecordingFile', 'ProviderFile', 'RemoteUrl'], record_audio: true, ...(options.policy || {}) },
    limits: { ...limits, ...(options.limits || {}) }, expires_at: new Date(wall() + (options.lifetimeMs ?? 3600000)).toISOString(), transfer_modes: ['authenticated_stream'] });
  const base = {
    async bootstrap(body, key) { calls.push({ op: 'bootstrap', key, body }); return session(body.capture_session_id); },
    async renewSession(current, sequence) { calls.push({ op: 'renewSession', sequence }); return session(current.capture_session_id); },
    async initialize(_s, body, key) {
      const request = JSON.parse(body); calls.push({ op: 'initialize', key, request, body }); requests.push(request);
      return { upload_id: id(), attachment_id: request.attachment_id, occurrence_id: request.occurrence_id, state: 'pending_upload', reserved_bytes: request.declared_bytes,
        expires_at: new Date(wall() + (options.uploadLifetimeMs ?? 600000)).toISOString(), transfer_mode: 'authenticated_stream', write_capability: cap() };
    },
    async renewUpload(_s, upload, body) {
      calls.push({ op: 'renewUpload', body });
      return { ...upload, write_capability: cap(), expires_at: new Date(Date.parse(upload.expires_at) + 600000).toISOString() };
    },
    async putContent(_s, upload, source) {
      const bytes = await readAll(source);
      calls.push({ op: 'putContent', upload: upload.upload_id, occurrence: upload.occurrence_id, size: bytes.length, capability: upload.write_capability });
      contents.set(upload.occurrence_id, bytes);
    },
    async complete(_s, upload, key) { calls.push({ op: 'complete', key, capability: upload.write_capability, occurrence: upload.occurrence_id }); completed.set(upload.occurrence_id, upload.write_capability); return 'validating'; },
    async abort(_s, upload, key) { calls.push({ op: 'abort', key, occurrence: upload.occurrence_id }); },
    async uploadStatus() { calls.push({ op: 'uploadStatus' }); return { state: 'validating', reason: 'binding_pending' }; },
    async putManifest(_s, capability, recordingId, body, key) { calls.push({ op: 'putManifest', key, capability, recordingId }); manifests.push({ capability, key, raw: body, body: JSON.parse(body) }); },
    async submitImport(_s, body, key) { const parsed = JSON.parse(body); calls.push({ op: 'submitImport', key, body: parsed }); imports.push({ raw: body, body: parsed, key }); return { jobId: id(), recordingId: parsed.recording.recording_id, state: 'importing' }; },
    async importStatus(_s, jobId) { calls.push({ op: 'importStatus', jobId }); return { state: 'importing', reason: 'binding_pending' }; },
  };
  const transport = { ...base, ...(typeof overrides === 'function' ? overrides(base) : overrides) };
  return { transport, base, calls, contents, manifests, imports, requests, completed, ops: () => calls.map(c => c.op) };
}
async function managerV1(overrides, options = {}, managerOptions = {}) {
  const double = v1(overrides, options);
  const manager = new core.AttachmentManager({ enabled: true, projectName: 'fixture', projectType: 'observe', environment: () => ({}), transport: double.transport,
    recordAudio: true, ...(options.clock ? { clock: options.clock } : {}), ...managerOptions });
  if (options.ready !== false) { if (!await manager.ready()) throw Error('ready'); }
  return { manager, ...double };
}
function span(context = identity) {
  const attrs = {}; let recording = true; let writes = 0;
  return { attrs, get writes() { return writes; }, end() { recording = false; },
    spanContext: () => context, isRecording: () => recording, setAttribute(k, v) { writes++; attrs[k] = v; return this; } };
}
/** Deterministic s16le PCM: `frames` frames of `channels` channels, offset by `seed`. */
function pcm(frames, channels = 1, seed = 0) {
  const out = new Uint8Array(frames * channels * 2);
  for (let i = 0; i < out.length; i++) out[i] = (i * 31 + seed * 7 + (i >> 9)) & 0xff;
  return out;
}
/** Write `seconds` of audio in `chunkMs` chunks; awaits `manager.flush()` every `flushEvery` seconds so uploads progress. */
async function writeSeconds(manager, writer, rate, seconds, { channels = 1, track = 0, chunkMs = 20, flushEvery = 10, seed = 0 } = {}) {
  const frames = rate * chunkMs / 1000; let accepted = 0; let rejected = 0; let offset = 0;
  const chunks = Math.round(seconds * 1000 / chunkMs);
  for (let i = 0; i < chunks; i++) {
    const data = pcm(frames, channels, seed + offset); offset += frames;
    if (writer.tryWrite(data, track)) accepted++; else rejected++;
    if (flushEvery && (i + 1) % Math.round(flushEvery * 1000 / chunkMs) === 0) await manager.flush(10000);
  }
  return { accepted, rejected };
}
const mono = rate => ({ format: { sampleRateHz: rate }, layout: 'mixed', tracks: [{ channels: [0], direction: 'mixed', role: 'mixed' }], operation: 'observe_consumed' });
module.exports = { core, nodeCore, v1, managerV1, span, pcm, writeSeconds, readAll, clock, mono };
