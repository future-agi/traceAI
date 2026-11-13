// Run against an esbuild browser bundle with Playwright installed in the qualification tools directory.
const assert = require('node:assert/strict');
const { chromium } = require(process.argv[2]);
const bundle = process.argv[3];
const id = '10000000-0000-4000-8000-000000000001';
const expires = new Date(Date.now() + 3600000).toISOString();
const policy = { version: 'v1', enabled: true, allowed_sources: ['Bytes', 'Blob'], allowed_modalities: ['audio', 'image'],
  require_media_redaction: false, require_metadata_removal: false, allow_unredacted_media_with_text_pii: false, record_audio: false, record_video: false };
const limits = { max_upload_bytes: 52428800, segment_bytes: 8388608, segment_duration_ms: 30000, recording_bytes: 1073741824,
  recording_duration_ms: 7200000, active_transfers: 16, active_recordings: 4, reserved_bytes: 1073741824, validation_jobs: 1024,
  initializations_per_minute: 120, links_per_minute: 1000, upload_bytes_per_second: 1073741824, playback_bytes_per_second: 1073741824,
  transfer_timeout_seconds: 600, grant_ttl_seconds: 300, revocation_check_seconds: 5 };
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage(); const calls = []; let content;
    await page.route('https://attachments.example/**', async route => {
      const req = route.request(); const url = req.url(); calls.push(url);
      const data = req.postDataBuffer(); let response; let status = 200;
      if (url.endsWith('attachment-sessions')) response = { schema_version: 1, scope: { organization_id: id, workspace_id: id, project_id: id },
        capture_session_id: JSON.parse(data).capture_session_id, policy, limits, expires_at: expires, transfer_modes: ['authenticated_stream'] };
      else if (url.endsWith('/uploads')) { const r = JSON.parse(data); status = 201; response = { upload_id: id, attachment_id: r.attachment_id,
        occurrence_id: r.occurrence_id, state: 'pending_upload', reserved_bytes: r.declared_bytes, expires_at: expires, transfer_mode: 'authenticated_stream', write_capability: 'synthetic-write-capability' }; }
      else if (url.endsWith('/content')) { content = [...data]; response = { upload_id: id, state: 'pending_upload', reason: 'queued' }; }
      else { status = 202; response = { upload_id: id, state: 'validating', reason: 'binding_pending' }; }
      await route.fulfill({ status, headers: { 'content-type': 'application/json', 'access-control-allow-origin': '*' }, body: JSON.stringify(response) });
    });
    await page.addScriptTag({ path: bundle });
    const result = await page.evaluate(async () => {
      const { AttachmentManager, createAttachmentHttpTransport } = Attachments;
      const identity = { traceId: '0123456789abcdef0123456789abcdef', spanId: '0123456789abcdef', traceFlags: 1 };
      const transport = createAttachmentHttpTransport({ endpoint: 'https://attachments.example', headers: { authorization: 'Bearer synthetic-write-capability' } });
      const manager = new AttachmentManager({ enabled: true, projectName: 'browser-fixture', projectType: 'observe', transport });
      if (!await manager.ready()) throw Error('bootstrap failed');
      const result = manager.attach(identity, { kind: 'Blob', data: new Blob([new Uint8Array([1, 2, 3])]) }, { direction: 'input', mimeType: 'image/png' });
      const completion = await result.completion; await manager.shutdown(); return completion;
    });
    assert.deepEqual(result, { status: 'validating' }); assert.deepEqual(content, [1, 2, 3]); assert.equal(calls.length, 4);
    const denied = await page.evaluate(async () => {
      const { AttachmentManager, createAttachmentHttpTransport } = Attachments;
      const transport = createAttachmentHttpTransport({ endpoint: 'https://attachments.example', headers: { authorization: 'Bearer synthetic-write-capability' } });
      const identity = { traceId: '0123456789abcdef0123456789abcdef', spanId: '0123456789abcdef', traceFlags: 1 };
      const states = [];
      for (const options of [{}, { enabled: true, traceConfig: { piiRedaction: true } }, { enabled: true, traceConfig: { hideInputImages: true } }]) {
        const manager = new AttachmentManager({ projectName: 'browser-fixture', projectType: 'observe', transport, ...options });
        await manager.ready(); let touched = false;
        const h = manager.attach(identity, { kind: 'Blob', get data() { touched = true; throw Error('denied-source'); } }, { direction: 'input', mimeType: 'image/png' });
        states.push([h.admission, touched]); await manager.shutdown();
      }
      return states;
    });
    assert.deepEqual(denied, [['disabled', false], ['redaction_unavailable', false], ['policy_denied', false]]);
    assert.equal(calls.length, 6); // two bootstrap controls; ZERO denied transfers.
    console.log(JSON.stringify({ browser: browser.version(), passed: 4, transferRequests: 3, deniedTransferRequests: 0 }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
