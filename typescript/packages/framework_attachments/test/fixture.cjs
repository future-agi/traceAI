const http = require('node:http');
const { randomUUID } = require('node:crypto');
const { once } = require('node:events');
const { AttachmentManager, createAttachmentHttpTransport, attachmentFetchClient } = require('@traceai/fi-core/attachments');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5nQAAAAASUVORK5CYII=', 'base64');
const URI = `data:image/png;base64,${PNG.toString('base64')}`;
// Status bodies carry the F01 `reason` member; the strict v1 transport rejects a status without it.
// Session and upload expiry sit an hour out so no renewal runs inside a test.
const expiry = () => new Date(Date.now() + 3600000).toISOString();
const limits = { max_upload_bytes: 52428800, segment_bytes: 8388608, segment_duration_ms: 30000, recording_bytes: 1073741824,
  recording_duration_ms: 7200000, active_transfers: 16, active_recordings: 4, reserved_bytes: 1073741824, validation_jobs: 1024,
  initializations_per_minute: 120, links_per_minute: 1000, upload_bytes_per_second: 1048576, playback_bytes_per_second: 1048576,
  transfer_timeout_seconds: 120, grant_ttl_seconds: 60, revocation_check_seconds: 5 };
async function fixture(config = {}) {
  const requests = [], bodies = [], uploads = [], completed = [];
  const server = http.createServer(async (req, res) => {
    try {
      const chunks = []; for await (const chunk of req) chunks.push(chunk);
      const body = Buffer.concat(chunks);
      requests.push({ method: req.method, path: req.url });
      res.setHeader('content-type', 'application/json');
      if (req.url.endsWith('/attachment-sessions')) {
        const request = JSON.parse(body);
        res.end(JSON.stringify({ schema_version: 1, capture_session_id: request.capture_session_id,
          scope: { organization_id: randomUUID(), workspace_id: randomUUID(), project_id: randomUUID() }, limits,
          policy: { version: 'test_v1', enabled: true, allowed_sources: ['Bytes'], allowed_modalities: ['image', 'audio', 'video', 'document', 'file'],
            require_media_redaction: false, require_metadata_removal: false, allow_unredacted_media_with_text_pii: false,
            record_audio: false, record_video: false, ...config.serverPolicy },
          expires_at: expiry(), transfer_modes: ['authenticated_stream'] }));
      } else if (req.url.endsWith('/uploads')) {
        const request = JSON.parse(body); uploads.push(request); res.statusCode = 201;
        res.end(JSON.stringify({ upload_id: randomUUID(), attachment_id: request.attachment_id, occurrence_id: request.occurrence_id,
          state: 'pending_upload', reserved_bytes: request.declared_bytes, expires_at: expiry(),
          transfer_mode: 'authenticated_stream', write_capability: 'synthetic-capability-only' }));
      } else {
        const upload_id = req.url.split('/').at(-2);
        if (req.url.endsWith('/content')) { bodies.push(body); res.end(JSON.stringify({ upload_id, state: 'pending_upload', reason: 'queued' })); }
        else { completed.push(upload_id); res.statusCode = 202; res.end(JSON.stringify({ upload_id, state: 'validating', reason: 'binding_pending' })); }
      }
    } catch { res.statusCode = 500; res.end('{}'); }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const manager = new AttachmentManager({ enabled: true, projectName: 'synthetic-framework', projectType: 'observe',
    transport: createAttachmentHttpTransport({ endpoint: 'https://attachments.invalid', headers: { 'x-api-key': 'synthetic-test' },
      client: request => attachmentFetchClient({ ...request, url: `http://127.0.0.1:${server.address().port}${new URL(request.url).pathname}` }),
    }), ...config, environment: () => config.environment ?? {} });
  await manager.ready();
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const tracer = provider.getTracer('framework-media-test');
  return { manager, provider, tracer, exporter, requests, bodies, uploads, completed,
    async close() { await manager.shutdown(); await provider.shutdown(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); } };
}
module.exports = { fixture, PNG, URI };
