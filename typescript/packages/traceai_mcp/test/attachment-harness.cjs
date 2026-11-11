const https = require('node:https');
const fs = require('node:fs');
const { randomUUID } = require('node:crypto');
const { once } = require('node:events');
const { createNodeAttachmentManager } = require('@traceai/fi-core/attachments/node');
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5XcAAAAASUVORK5CYII=', 'base64');
const WAV = Buffer.alloc(48); WAV.write('RIFF'); WAV.writeUInt32LE(40, 4); WAV.write('WAVEfmt ', 8); WAV.writeUInt32LE(16, 16); WAV.writeUInt16LE(1, 20); WAV.writeUInt16LE(1, 22); WAV.writeUInt32LE(8000, 24); WAV.writeUInt32LE(16000, 28); WAV.writeUInt16LE(2, 32); WAV.writeUInt16LE(16, 34); WAV.write('data', 36); WAV.writeUInt32LE(4, 40);
const PDF = Buffer.from('%PDF-1.7\nsynthetic fixture\n%%EOF');
// Every FI_ATTACHMENTS_* process warning (legacy transport, rejected wire shape) seen by this process.
const warnings = [];
process.on('warning', warning => { if (String(warning.code || '').startsWith('FI_ATTACHMENTS_')) warnings.push(warning.code); });
const identity = { traceId: '0123456789abcdef0123456789abcdef', spanId: '0123456789abcdef', traceFlags: 1 };

async function harness(options = {}) {
  const requests = [], uploads = [], records = [];
  const policy = { version: '1', enabled: true, allowed_sources: ['Bytes', 'Blob'], allowed_modalities: ['audio', 'image', 'video', 'document', 'file'], require_media_redaction: false, require_metadata_removal: false, allow_unredacted_media_with_text_pii: false, record_audio: false, record_video: false, ...options.serverPolicy };
  const scope = { organization_id: randomUUID(), workspace_id: randomUUID(), project_id: randomUUID() };
  const server = https.createServer({ key: fs.readFileSync(process.env.ATTACHMENT_TEST_KEY), cert: fs.readFileSync(process.env.NODE_EXTRA_CA_CERTS) }, async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks);
    requests.push({ path: req.url, headers: req.headers });
    res.setHeader('content-type', 'application/json');
    if (req.url === '/tracer/v1/attachment-sessions') {
      const request = JSON.parse(body);
      res.end(JSON.stringify({ schema_version: 1, scope, capture_session_id: request.capture_session_id, policy, limits: { max_upload_bytes: 1048576, segment_bytes: 8388608, segment_duration_ms: 30000, recording_bytes: 1073741824, recording_duration_ms: 7200000, active_transfers: 16, active_recordings: 4, reserved_bytes: 1073741824, validation_jobs: 1024, initializations_per_minute: 120, links_per_minute: 1000, upload_bytes_per_second: 1048576, playback_bytes_per_second: 1048576, transfer_timeout_seconds: 600, grant_ttl_seconds: 300, revocation_check_seconds: 5, ...options.limits }, expires_at: new Date(Date.now() + 3600000).toISOString(), transfer_modes: ['authenticated_stream'] }));
    } else if (req.url === `/tracer/v1/projects/${scope.project_id}/attachments/uploads`) {
      const request = JSON.parse(body); records.push(request);
      res.statusCode = 201;
      res.end(JSON.stringify({ upload_id: randomUUID(), attachment_id: request.attachment_id, occurrence_id: request.occurrence_id, state: 'pending_upload', reserved_bytes: request.declared_bytes, expires_at: new Date(Date.now() + 600000).toISOString(), transfer_mode: 'authenticated_stream', write_capability: 'synthetic-write-capability' }));
    } else if (req.url.endsWith('/content')) {
      uploads.push(body); res.end(JSON.stringify({ upload_id: req.url.split('/').at(-2), state: 'pending_upload' }));
    } else if (req.url.endsWith('/complete')) {
      res.statusCode = 202; res.end(JSON.stringify({ upload_id: req.url.split('/').at(-2), state: 'validating', reason: 'binding_pending' }));
    } else { res.statusCode = 404; res.end('{}'); }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const manager = createNodeAttachmentManager({ enabled: true, projectName: 'fixture', projectType: 'observe', endpoint: `https://127.0.0.1:${server.address().port}`, headers: { authorization: 'Bearer synthetic-bootstrap' }, ...options.manager });
  await manager.ready();
  const exporter = new InMemorySpanExporter();
  const processor = new SimpleSpanProcessor(exporter);
  const provider = new BasicTracerProvider({ spanProcessors: [processor] });
  provider.attachments = manager;
  return { manager, provider, exporter, requests, uploads, records, async close() {
    await manager.shutdown(); await provider.shutdown(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  } };
}
module.exports = { harness, PNG, WAV, PDF, identity, warnings };
