/*
 * Stacked-lane compatibility: API changes that used to break callers SILENTLY (capture just stopped) must now be loud:
 * a compile error, a one-time process warning with a stable code, or an explicit reason at the call. Synthetic data only.
 * See ATTACHMENTS-MIGRATION.md.
 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { core, clock, mono, pcm } = require('./attachments-v1.cjs');
const { identity, ids, policy, limits, fake } = require('./attachments-fixtures.cjs');
const { AttachmentManager } = core;
const tick = () => new Promise(resolve => setImmediate(resolve));
const helpers = { v1: require.resolve('./attachments-v1.cjs'), fixtures: require.resolve('./attachments-fixtures.cjs') };

/** Runs `body` in a fresh Node process (warnings are once per process) and returns its stderr and last stdout line. */
function isolated(body) {
  const script = `
    const { core, managerV1, clock, mono, pcm } = require(${JSON.stringify(helpers.v1)});
    const { identity, ids, policy, limits, fake } = require(${JSON.stringify(helpers.fixtures)});
    const out = {}; const warnings = [];
    process.on('warning', w => warnings.push({ name: w.name, code: w.code }));
    (async () => { ${body}
      await new Promise(r => setImmediate(r));
      console.log(JSON.stringify({ out, warnings }));
    })().catch(e => { console.error(e && e.stack); process.exit(3); });`;
  const r = spawnSync(process.execPath, ['-e', script], { env: process.env, encoding: 'utf8', timeout: 20000 });
  assert.equal(r.status, 0, r.stderr);
  return { stderr: r.stderr, ...JSON.parse(r.stdout.trim().split('\n').pop()) };
}
const legacySession = sessionPolicy => ({
  async bootstrap(r) {
    return { schema_version: 1, scope: { organization_id: ids[0], workspace_id: ids[1], project_id: ids[2] }, capture_session_id: r.capture_session_id,
      policy: { ...policy, ...sessionPolicy }, limits, expires_at: new Date(Date.now() + 3600000).toISOString(), transfer_modes: ['authenticated_stream'] };
  },
});

test('C-01 a legacy four-method transport warns ONCE per process (DeprecationWarning FI_ATTACHMENTS_LEGACY_TRANSPORT) and names the fix', () => {
  const r = isolated(`
    for (let i = 0; i < 3; i++) {
      const m = new core.AttachmentManager({ enabled: true, projectName: 'fixture', projectType: 'observe', environment: () => ({}), transport: fake().transport });
      out['ready' + i] = await m.ready(); await m.shutdown(100);
    }`);
  assert.deepEqual(r.warnings, [{ name: 'DeprecationWarning', code: 'FI_ATTACHMENTS_LEGACY_TRANSPORT' }]);
  assert.match(r.stderr, /FI_ATTACHMENTS_LEGACY_TRANSPORT/);
  assert.match(r.stderr, /createAttachmentHttpTransportV1/); assert.match(r.stderr, /ATTACHMENT_TRANSPORT_V1/); assert.match(r.stderr, /ATTACHMENTS-MIGRATION\.md/);
  assert.deepEqual(r.out, { ready0: true, ready1: true, ready2: true }, 'the legacy shape still works for single uploads');
});

test('C-02 no warning for an AttachmentTransportV1, for createAttachmentHttpTransport (carries V1), or when attachments are OFF', () => {
  const r = isolated(`
    const http = core.createAttachmentHttpTransport({ endpoint: 'https://attachments.example', headers: {}, client: async () => ({ status: 503, body: new Uint8Array() }) });
    const f = await managerV1(); await f.manager.shutdown(100);
    const h = new core.AttachmentManager({ enabled: true, projectName: 'fixture', projectType: 'observe', environment: () => ({}), transport: http }); await h.shutdown(100);
    const off = new core.AttachmentManager({ transport: fake().transport }); await off.shutdown(100);`);
  assert.deepEqual(r.warnings, []);
  assert.doesNotMatch(r.stderr, /FI_ATTACHMENTS_/);
});

test('C-03 a wrapper that drops ATTACHMENT_TRANSPORT_V1 is detected as legacy (the coding-lane guard shape) and warns', () => {
  const r = isolated(`
    const http = core.createAttachmentHttpTransport({ endpoint: 'https://attachments.example', headers: {}, client: async () => ({ status: 503, body: new Uint8Array() }) });
    const guarded = { bootstrap: (...a) => http.bootstrap(...a), initialize: (...a) => http.initialize(...a), upload: (...a) => http.upload(...a), complete: (...a) => http.complete(...a) };
    const m = new core.AttachmentManager({ enabled: true, projectName: 'fixture', projectType: 'observe', environment: () => ({}), transport: guarded }); await m.shutdown(100);
    const forwarded = { ...guarded, [core.ATTACHMENT_TRANSPORT_V1]: http[core.ATTACHMENT_TRANSPORT_V1] };
    out.forwardedIsV1 = typeof forwarded[core.ATTACHMENT_TRANSPORT_V1].putContent === 'function';`);
  assert.deepEqual(r.warnings, [{ name: 'DeprecationWarning', code: 'FI_ATTACHMENTS_LEGACY_TRANSPORT' }]);
  assert.equal(r.out.forwardedIsV1, true);
});

test('C-04 legacy wire: recording, WAV import and completed-recording import are denied up front with unsupported_version; single uploads still work', async () => {
  const f = fake(legacySession({ allowed_sources: ['Bytes', 'Blob', 'OwnedRecordingFile', 'ProviderFile', 'RemoteUrl'], record_audio: true }));
  const m = new AttachmentManager({ enabled: true, projectName: 'fixture', projectType: 'observe', environment: () => ({}), transport: f.transport, recordAudio: true });
  assert.equal(await m.ready(), true);
  const w = m.startRecording(identity, mono(8000));
  assert.equal(w.reason, 'unsupported_version'); assert.equal(w.tryWrite(pcm(160)), false);
  assert.equal((await w.finish(100)).state, 'denied');
  let resolverCalls = 0;
  const imp = await m.importCompletedRecording(identity, { provider: 'livekit_egress', providerRef: 'EG_synthetic' }, { resolver: { async resolve() { resolverCalls++; return 'source_unavailable'; } } });
  assert.equal(imp.reason, 'unsupported_version'); assert.equal(resolverCalls, 0, 'the resolver is never called');
  assert.equal((await imp.completion).status, 'unsupported_version');
  assert.equal((await m.attach(identity, { kind: 'Bytes', data: new Uint8Array([1, 2]) }, { mimeType: 'image/png', direction: 'input' }).completion).status, 'validating');
  assert.deepEqual(f.calls, ['initialize', 'upload', 'complete'], 'only the single upload reached the transport (bootstrap is the override)');
  await m.flush(1000); // the finished job releases its bytes just after its completion settles
  assert.equal(m.status().ownedBytes, 0); assert.equal(m.status().openRecordings, 0);
  await m.shutdown(100);
});

test('C-05 deny-wins: on a legacy wire a policy denial still beats unsupported_version', async () => {
  const f = fake(legacySession({ record_audio: false }));
  const m = new AttachmentManager({ enabled: true, projectName: 'fixture', projectType: 'observe', environment: () => ({}), transport: f.transport, traceConfig: { hideInputs: true } });
  assert.equal(await m.ready(), true);
  assert.equal(m.startRecording(identity, mono(8000)).reason, 'policy_denied');
  assert.ok(['policy_denied', 'recording_disabled'].includes(m.startRecording(identity, { ...mono(8000), operation: 'create_recording' }).reason));
  await m.shutdown(100);
  const off = new AttachmentManager({ transport: fake().transport });
  assert.equal(off.startRecording(identity, mono(8000)).reason, 'disabled');
});

test('C-06 legacy wire: no renewal reaches the transport; the session runs to `expired` and capture says so', async () => {
  const c = clock(Date.now()); let boots = 0;
  const f = fake({ async bootstrap(r) { boots++; return { ...(await legacySession({}).bootstrap(r)), expires_at: new Date(c.wall() + 120000).toISOString() }; } });
  const m = new AttachmentManager({ enabled: true, projectName: 'fixture', projectType: 'observe', environment: () => ({}), transport: f.transport, clock: c });
  assert.equal(await m.ready(), true);
  c.advance(100000); m.status(); await m.flush(100); for (let i = 0; i < 10; i++) await tick();
  assert.equal(m.status().state, 'ready'); assert.equal(boots, 1);
  c.advance(30000);
  assert.equal(m.status().state, 'expired');
  assert.equal(m.attach(identity, { kind: 'Bytes', data: new Uint8Array([1]) }, { mimeType: 'image/png', direction: 'input' }).admission, 'expired');
  await m.shutdown(100);
});

test('C-07 a response failing its F01 shape (status body without `reason`) is unsupported_version AND warns once with the shape name', () => {
  const r = isolated(`
    let n = 0;
    const session = sid => ({ schema_version: 1, scope: { organization_id: ids[0], workspace_id: ids[1], project_id: ids[2] }, capture_session_id: sid,
      policy, limits, expires_at: new Date(Date.now() + 3600000).toISOString(), transfer_modes: ['authenticated_stream'] });
    const client = async req => {
      const p = new URL(req.url).pathname; const json = (status, body) => ({ status, body: Buffer.from(JSON.stringify(body)) });
      if (p.endsWith('/attachment-sessions')) return json(200, session(JSON.parse(req.body).capture_session_id));
      if (p.endsWith('/uploads')) { const b = JSON.parse(req.body); return json(201, { upload_id: '50000000-0000-4000-8000-00000000000' + (++n), attachment_id: b.attachment_id,
        occurrence_id: b.occurrence_id, state: 'pending_upload', reserved_bytes: b.declared_bytes, expires_at: new Date(Date.now() + 600000).toISOString(),
        transfer_mode: 'authenticated_stream', write_capability: 'synthetic-capability-' + n + '-abcdef' }); }
      if (p.endsWith('/content')) return json(200, { upload_id: p.split('/')[7], state: 'pending_upload' }); // no reason: the pre-F01 double shape
      if (p.endsWith('/abort')) return json(200, { upload_id: p.split('/')[7], state: 'failed', reason: 'incomplete' });
      return { status: 404, body: new Uint8Array() };
    };
    const transport = core.createAttachmentHttpTransportV1({ endpoint: 'https://attachments.example', headers: { authorization: 'Bearer synthetic-original' }, client });
    const m = new core.AttachmentManager({ enabled: true, projectName: 'fixture', projectType: 'observe', environment: () => ({}), transport });
    out.ready = await m.ready();
    const a = m.attach(identity, { kind: 'Bytes', data: new Uint8Array([1, 2]) }, { mimeType: 'image/png', direction: 'input' });
    const b = m.attach(identity, { kind: 'Bytes', data: new Uint8Array([3, 4]) }, { mimeType: 'image/png', direction: 'input' });
    out.a = (await a.completion).status; out.b = (await b.completion).status;
    await m.shutdown(100);`);
  assert.equal(r.out.ready, true);
  assert.equal(r.out.a, 'unsupported_version'); assert.equal(r.out.b, 'unsupported_version');
  assert.deepEqual(r.warnings, [{ name: 'Warning', code: 'FI_ATTACHMENTS_WIRE_REJECTED' }]);
  assert.match(r.stderr, /F01 UploadStatus check/);
  // Shape names only: no capability, credential, URL or body value reaches the warning.
  assert.doesNotMatch(r.stderr, /synthetic-capability|synthetic-original|attachments\.example|50000000-/);
});

test('C-08 type-level: retired values are compile errors, not silent never-true checks (tsc --noEmit on test/types)', { timeout: 120000 }, t => {
  let tsc;
  try { tsc = require.resolve('typescript/bin/tsc', { paths: [path.resolve(__dirname, '..')] }); }
  catch { t.skip('typescript is not installed next to this fi-core (packed-install run)'); return; }
  const project = path.join(__dirname, 'types', 'tsconfig.json');
  const r = spawnSync(process.execPath, [tsc, '--noEmit', '-p', project], { encoding: 'utf8', timeout: 110000 });
  // Every `@ts-expect-error` in the file must be consumed: an unused one (TS2578) means the retired value compiles again.
  assert.equal(r.status, 0, `${r.stdout}\n${r.stderr}`);
});
