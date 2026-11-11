/* Loopback attachment control plane plus real LiveKit Agents (Node) objects. Synthetic data only. */
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { once } = require('node:events');
const { randomUUID } = require('node:crypto');
const { AttachmentManager, createAttachmentHttpTransport } = require('@traceai/fi-core/attachments');

const ids = [1, 2, 3].map(n => `10000000-0000-4000-8000-00000000000${n}`);
// Sessions of at least 1 h, so the core's near-expiry renewal never adds a bootstrap here.
const expiry = () => new Date(Date.now() + 3600000).toISOString();
const SLICE = 256 * 1024;

async function readBody(body) {
  if (body === undefined || typeof body === 'string' || body instanceof Uint8Array) return body;
  if (typeof Blob !== 'undefined' && body instanceof Blob) return Buffer.from(await body.arrayBuffer());
  // Ranged owned-file source: check before every slice and after the last, like the Node client.
  const out = Buffer.alloc(body.size);
  for (let offset = 0; offset < body.size; offset += SLICE) {
    await body.check();
    const chunk = await body.read(offset, Math.min(SLICE, body.size - offset));
    Buffer.from(chunk).copy(out, offset);
  }
  await body.check();
  return out;
}

async function attachmentFixture(config = {}, serverPolicy = {}) {
  const calls = [], uploaded = [], requests = [], uploads = new Map();
  let hold; const held = [];
  const policy = { version: 'v1', enabled: true, allowed_sources: ['Bytes', 'Blob', 'OwnedRecordingFile'], allowed_modalities: ['audio'],
    require_media_redaction: false, require_metadata_removal: false, allow_unredacted_media_with_text_pii: false,
    record_audio: false, record_video: false, ...serverPolicy };
  const server = http.createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const bytes = Buffer.concat(chunks);
    calls.push({ method: req.method, path: req.url });
    res.setHeader('content-type', 'application/json');
    if (req.url.endsWith('/attachment-sessions')) {
      const body = JSON.parse(bytes);
      res.end(JSON.stringify({ schema_version: 1, scope: { organization_id: ids[0], workspace_id: ids[1], project_id: ids[2] },
        capture_session_id: body.capture_session_id, policy, limits: { max_upload_bytes: 8388608, segment_bytes: 8388608,
          segment_duration_ms: 30000, recording_bytes: 1073741824, recording_duration_ms: 7200000,
          active_transfers: 16, active_recordings: 4, reserved_bytes: 1073741824, validation_jobs: 1024,
          initializations_per_minute: 120, links_per_minute: 1000, upload_bytes_per_second: 1073741824,
          playback_bytes_per_second: 1073741824, transfer_timeout_seconds: 600, grant_ttl_seconds: 300, revocation_check_seconds: 5 },
        expires_at: expiry(), transfer_modes: ['authenticated_stream'] }));
    } else if (req.url.endsWith('/uploads')) {
      const body = JSON.parse(bytes); requests.push(body);
      const upload = { upload_id: randomUUID(), attachment_id: body.attachment_id, occurrence_id: body.occurrence_id,
        state: 'pending_upload', reserved_bytes: body.declared_bytes, expires_at: expiry(),
        transfer_mode: 'authenticated_stream', write_capability: 'synthetic-write-capability' };
      uploads.set(upload.upload_id, upload); res.statusCode = 201; res.end(JSON.stringify(upload));
    } else {
      const id = req.url.split('/').at(-2);
      if (!uploads.has(id) || req.headers.authorization !== 'Bearer synthetic-write-capability') { res.statusCode = 403; res.end('{}'); return; }
      if (req.url.endsWith('/content')) {
        if (hold) { const gate = hold; hold = undefined; held.push(id); await gate; }
        uploaded.push(bytes); res.end(JSON.stringify({ upload_id: id, state: 'pending_upload', reason: 'queued' }));
      } else {
        res.statusCode = 202; res.end(JSON.stringify({ upload_id: id, state: 'validating', reason: 'binding_pending' }));
      }
    }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const client = async req => {
    const body = await readBody(req.body);
    return new Promise((resolve, reject) => {
      const r = http.request(`http://127.0.0.1:${server.address().port}${new URL(req.url).pathname}`,
        { method: req.method, headers: req.headers, signal: req.signal }, response => {
          const chunks = []; response.on('data', c => chunks.push(c));
          response.on('end', () => resolve({ status: response.statusCode, body: Buffer.concat(chunks) }));
        });
      r.on('error', reject); r.end(body);
    });
  };
  const manager = new AttachmentManager({ enabled: true, projectName: 'synthetic-voice', projectType: 'observe',
    transport: createAttachmentHttpTransport({ endpoint: 'https://attachments.example', headers: { authorization: 'Bearer synthetic' }, client }), ...config });
  await manager.ready();
  return { manager, calls, uploaded, requests, held,
    /** Hold the next content PUT until the returned release() is called. */
    holdNext() { let release; hold = new Promise(r => { release = r; }); return () => release(); },
    async close() { await manager.shutdown(1000); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); } };
}

/** Real `@livekit/agents` objects from an installed copy (LIVEKIT_AGENTS_DIR). */
function loadAgents(dir) {
  const main = require.resolve('@livekit/agents', { paths: [dir] });
  const agents = require(main);
  try { agents.initializeLogger({ pretty: false, level: 'error' }); } catch { /* already initialized */ }
  // RecorderIO is internal to the SDK; load the real class from the installed copy's dist.
  const dist = main.slice(0, main.lastIndexOf(`${path.sep}dist${path.sep}`) + 6);
  const { RecorderIO } = require(path.join(dist, 'voice', 'recorder_io', 'index.cjs'));
  const rtc = require(require.resolve('@livekit/rtc-node', { paths: [path.dirname(main)] }));
  return { agents, RecorderIO, AudioFrame: rtc.AudioFrame, rtc };
}

let jobs = 0;
/**
 * A real JobContext and AgentSession in the state `AgentSession.start({ record: true })` leaves them:
 * primary session, audio recording option on, session span context, and the SDK's own RecorderIO
 * writing `${ctx.sessionDirectory}/audio.ogg`.
 */
async function startedSession(sdk, tracer, { redaction = false, audio = true, record = true } = {}) {
  const { agents, RecorderIO } = sdk;
  const { EventEmitter } = require('node:events');
  const room = new EventEmitter(); room.isConnected = false;
  const jobId = `AJ_synthetic_${process.pid}_${++jobs}`;
  const info = { job: { id: jobId, room: { name: 'synthetic-room', sid: 'RM_synthetic' }, enableRecording: false, enableRedaction: redaction, agentName: '' },
    url: 'ws://127.0.0.1:9', acceptArguments: {}, token: 'synthetic', workerId: 'synthetic' };
  const ctx = new agents.JobContext({ userData: {} }, info, room, () => {}, () => {}, {});
  const session = new agents.voice.AgentSession({});
  session.sessionOptions.recordingOptions = { ...session.sessionOptions.recordingOptions, audio, redaction: false };
  ctx._primaryAgentSession = session;
  const span = tracer.startSpan('agent_session');
  const { trace, ROOT_CONTEXT } = require('@opentelemetry/api');
  session.rootSpanContext = trace.setSpan(ROOT_CONTEXT, span);
  let recorder;
  if (record) {
    class In extends agents.voice.AudioInput {}
    class Out extends agents.voice.AudioOutput {
      constructor() { super(48000); }
      flush() { super.flush(); setTimeout(() => this.onPlaybackFinished({ playbackPosition: 0.5, interrupted: false }), 20); }
      clearBuffer() {}
    }
    recorder = new RecorderIO({ agentSession: session });
    recorder.recordInput(new In()); const out = recorder.recordOutput(new Out());
    await recorder.start(path.join(ctx.sessionDirectory, 'audio.ogg'));
    session._recorderIO = recorder;
    const n = 24000, data = new Int16Array(n);
    for (let i = 0; i < n; i++) data[i] = Math.round(8000 * Math.sin(2 * Math.PI * 1000 * i / 48000));
    await out.captureFrame(new sdk.AudioFrame(data, 48000, 1, n)); out.flush();
    await new Promise(resolve => setTimeout(resolve, 700));
  }
  return { ctx, session, span, recorder, file: path.join(ctx.sessionDirectory, 'audio.ogg'),
    cleanup() { try { span.end(); } catch { /* ended */ } fs.rmSync(ctx.sessionDirectory, { recursive: true, force: true }); } };
}

module.exports = { attachmentFixture, loadAgents, startedSession };
