const http = require('node:http');
const { once } = require('node:events');
const { randomUUID } = require('node:crypto');
const { AttachmentManager, createAttachmentHttpTransport } = require('@traceai/fi-core/attachments');
const ids = [1, 2, 3].map(n => `10000000-0000-4000-8000-00000000000${n}`);
// Sessions of at least 1 h, so the core's near-expiry renewal never adds a bootstrap here.
const expiry = () => new Date(Date.now() + 3600000).toISOString();

async function attachmentFixture(config = {}, serverPolicy = {}) {
  const calls = [], uploaded = [], requests = [], uploads = new Map();
  const policy = { version: 'v1', enabled: true, allowed_sources: ['Bytes', 'Blob'], allowed_modalities: ['audio'],
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
        uploaded.push(bytes); res.end(JSON.stringify({ upload_id: id, state: 'pending_upload', reason: 'queued' }));
      } else {
        res.statusCode = 202; res.end(JSON.stringify({ upload_id: id, state: 'validating', reason: 'binding_pending' }));
      }
    }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const client = async req => {
    const body = req.body instanceof Blob ? Buffer.from(await req.body.arrayBuffer()) : req.body;
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
  return { manager, calls, uploaded, requests,
    async close() { await manager.shutdown(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); } };
}

function wav() {
  const samples = 2400, out = Buffer.alloc(44 + samples * 2);
  out.write('RIFF'); out.writeUInt32LE(out.length - 8, 4); out.write('WAVEfmt ', 8);
  out.writeUInt32LE(16, 16); out.writeUInt16LE(1, 20); out.writeUInt16LE(1, 22);
  out.writeUInt32LE(24000, 24); out.writeUInt32LE(48000, 28); out.writeUInt16LE(2, 32); out.writeUInt16LE(16, 34);
  out.write('data', 36); out.writeUInt32LE(samples * 2, 40);
  for (let i = 0; i < samples; i++) out.writeInt16LE(Math.round(Math.sin(i * 2 * Math.PI * 440 / 24000) * 2000), 44 + i * 2);
  return out;
}
module.exports = { attachmentFixture, wav };
