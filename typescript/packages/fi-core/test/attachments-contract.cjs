// Runtime safety replay of every F01 privacy row. Unsupported positive paths
// are reported separately: fail-closed rejection is NOT integrated qualification.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { AttachmentManager, AttachmentCorrelation, AttachmentTransportError, canonicalAttachmentTraceId, attachmentTraceUuid } = require('../dist/src/attachments');
const { identity, policy, fake, source } = require('./attachments-fixtures.cjs');
const directory = process.argv[2];
if (!directory) throw Error('Pass the frozen spec/attachments/v1 directory');
const privacy = JSON.parse(fs.readFileSync(path.join(directory, 'privacy-fixtures.json'))).cases;
const identities = JSON.parse(fs.readFileSync(path.join(directory, 'identity-fixtures.json'))).cases;
const keepalive = setInterval(() => {}, 1000);
(async () => {
  const results = [];
  for (const fixture of privacy) {
    const c = fixture.context;
    const traceConfig = Object.fromEntries(['hide_inputs', 'hide_outputs', 'hide_input_messages', 'hide_output_messages', 'hide_input_images', 'hide_input_text', 'hide_output_text', 'hide_error_details', 'pii_redaction'].map(key => [key.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase()), c[key]]));
    const mimeType = { audio: 'audio/wav', image: 'image/png', video: 'video/mp4', document: 'application/pdf', file: 'application/octet-stream' }[c.modality];
    const options = { mimeType, direction: c.direction, capture: { captureContent: c.integration_capture_content, recordInputs: c.integration_record_inputs,
      recordOutputs: c.integration_record_outputs, muted: c.muted, temporaryChat: c.temporary_chat, requested: c.call_requested,
      modelInput: c.model_bound_image, containsInput: c.semantic_input, containsOutput: c.semantic_output, errorDetail: c.error_diagnostic,
      newRecording: c.operation === 'create_recording' } };
    const f = fake(); const requests = [];
    const transport = { ...f.transport,
      async bootstrap(request) {
        if (c.policy_known !== true) return new Promise(() => {});
        if (!c.credential_allowed) throw new AttachmentTransportError('credential_scope_unsupported');
        const result = await f.transport.bootstrap(request);
        result.policy = { ...policy, enabled: c.server_allowed,
          allowed_sources: c.source_authorized ? [c.source_kind] : [], allowed_modalities: c.modality_allowed ? [c.modality] : [],
          require_media_redaction: c.server_require_media_redaction, require_metadata_removal: c.require_metadata_removal,
          allow_unredacted_media_with_text_pii: c.server_allow_unredacted_media_with_text_pii, record_audio: c.server_record_audio, record_video: c.server_record_video };
        return result;
      },
      async initialize(session, request, signal) { requests.push(request.occurrence_id); return f.transport.initialize(session, request, signal); },
    };
    const manager = new AttachmentManager({ enabled: c.enabled, projectName: 'fixture', projectType: 'observe', traceConfig,
      recordAudio: c.record_audio, recordVideo: c.record_video, allowUnredactedMediaWithTextPii: c.allow_unredacted_media_with_text_pii,
      transport: c.endpoint_configured ? transport : undefined, maxOwnedBytes: c.budget_available ? undefined : 1,
      maxDescriptors: c.queue_available ? undefined : 1 });
    await manager.ready(1);
    if (!c.generation_current) manager.invalidate('deleted');
    if (!c.queue_available) manager.attach(identity, source(), options);
    const actualSource = c.browser_remote_fetch ? { kind: 'RemoteUrl', url: 'https://untrusted.example/source' } :
      c.source_kind === 'Bytes' ? { kind: 'Bytes', data: c.source_available ? source().data : undefined } : { kind: c.source_kind };
    const context = !c.correlation_available ? new AttachmentCorrelation() : { ...identity, traceFlags: c.sampled ? 1 : 0 };
    const result = manager.attach(context, actualSource, options);
    const unsupported = c.qualified_client_sanitizer || c.qualified_client_metadata_removal || ['RemoteUrl', 'OwnedRecordingFile', 'ProviderFile'].includes(c.source_kind);
    if (fixture.expected.decision === 'deny' || unsupported) {
      assert.notEqual(result.admission, 'queued', fixture.id);
      assert.equal(requests.includes(result.occurrenceId), false, fixture.id);
    } else assert.equal(result.admission, 'queued', fixture.id);
    results.push({ id: fixture.id, expected: fixture.expected.reason, actual: result.admission,
      qualification: fixture.expected.decision === 'allow' && unsupported ? 'unsupported_fail_closed' : 'safety_pass' });
    await manager.shutdown(10);
  }
  for (const fixture of identities) {
    const hex = canonicalAttachmentTraceId(fixture.input);
    assert.equal(hex !== undefined, fixture.valid, JSON.stringify(fixture.input));
    if (fixture.valid) assert.equal(attachmentTraceUuid(hex), fixture.canonical);
  }
  console.log(JSON.stringify({ privacyFixtures: results.length, identityFixtures: identities.length,
    exactReasons: results.filter(result => result.expected === result.actual).length,
    unsupportedPositive: results.filter(result => result.qualification === 'unsupported_fail_closed'),
    conservativeReasonDifferences: results.filter(result => result.expected !== result.actual && result.qualification !== 'unsupported_fail_closed') }, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => clearInterval(keepalive));
