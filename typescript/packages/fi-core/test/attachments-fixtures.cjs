const identity = { traceId: '0123456789abcdef0123456789abcdef', spanId: '0123456789abcdef', traceFlags: 1 };
const ids = ['10000000-0000-4000-8000-000000000001', '10000000-0000-4000-8000-000000000002', '10000000-0000-4000-8000-000000000003'];
const policy = { version: 'v1', enabled: true, allowed_sources: ['Bytes', 'Blob'], allowed_modalities: ['audio', 'image', 'video', 'document', 'file'],
  require_media_redaction: false, require_metadata_removal: false, allow_unredacted_media_with_text_pii: false, record_audio: false, record_video: false };
const limits = { max_upload_bytes: 52428800, segment_bytes: 8388608, segment_duration_ms: 30000, recording_bytes: 1073741824,
  recording_duration_ms: 7200000, active_transfers: 16, active_recordings: 4, reserved_bytes: 1073741824, validation_jobs: 1024,
  initializations_per_minute: 120, links_per_minute: 1000, upload_bytes_per_second: 1073741824, playback_bytes_per_second: 1073741824,
  transfer_timeout_seconds: 600, grant_ttl_seconds: 300, revocation_check_seconds: 5 };
const options = { mimeType: 'audio/wav', direction: 'mixed' };
const source = () => ({ kind: 'Bytes', data: new Uint8Array([82, 73, 70, 70, 1, 2, 3, 4]) });
const expiry = () => new Date(Date.now() + 60000).toISOString();
function fake(overrides = {}) {
  const calls = []; let body;
  const transport = {
    async bootstrap(r) { calls.push('bootstrap'); return { schema_version: 1, scope: { organization_id: ids[0], workspace_id: ids[1], project_id: ids[2] }, capture_session_id: r.capture_session_id, policy, limits, expires_at: expiry(), transfer_modes: ['authenticated_stream'] }; },
    async initialize(_s, r) { calls.push('initialize'); return { upload_id: ids[0], attachment_id: r.attachment_id, occurrence_id: r.occurrence_id, state: 'pending_upload', reserved_bytes: r.declared_bytes, expires_at: expiry(), transfer_mode: 'authenticated_stream', write_capability: 'synthetic-write-capability' }; },
    async upload(_s, _u, data) { calls.push('upload'); body = Array.from(data instanceof Uint8Array ? data : new Uint8Array(await data.arrayBuffer())); },
    async complete() { calls.push('complete'); }, ...overrides,
  };
  return { transport, calls, body: () => body };
}

module.exports = { identity, ids, policy, limits, options, source, expiry, fake };
