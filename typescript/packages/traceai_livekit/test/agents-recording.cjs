/*
 * V04: the session-recording binding against a REAL installed `@livekit/agents` (LIVEKIT_AGENTS_DIR):
 * real JobContext._onSessionEnd and makeSessionReport, real AgentSession, and the SDK's own RecorderIO
 * writing a real Ogg/Opus file. The control plane is a loopback HTTP double. Synthetic data only.
 */
const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { BasicTracerProvider } = require('@opentelemetry/sdk-trace-base');
const { attachmentFixture, loadAgents, startedSession } = require('./agents-fixture.cjs');

const dir = process.env.LIVEKIT_AGENTS_DIR;
if (!dir) throw new Error('LIVEKIT_AGENTS_DIR must point at an install of @livekit/agents');
const pkg = process.env.LIVEKIT_INSTRUMENTATION_DIR || path.join(__dirname, '..');
const { bindLiveKitSessionRecording, LIVEKIT_AGENTS_SUPPORTED_VERSIONS, LiveKitSessionRecording } =
  require(path.join(pkg, 'dist', 'src', 'agents.js'));
const sdk = loadAgents(dir);
const tracer = new BasicTracerProvider().getTracer('synthetic');
// The RTC runtime the SDK loads keeps native threads alive until disposed.
after(async () => { await sdk.rtc.dispose?.(); });
const ffprobe = (() => { try { execFileSync('ffprobe', ['-version'], { stdio: 'ignore' }); return true; } catch { return false; } })();

/** What the SDK's finalizeSession does: AgentSession.close() closes the recorder, then ctx._onSessionEnd(). */
async function endSession(s, { closeRecorder = true } = {}) {
  if (closeRecorder && s.recorder) await s.recorder.close();
  s.span.end();
  s.session._recordedEvents = ['synthetic-event'];
  await s.ctx._onSessionEnd();
}

test(`real @livekit/agents ${sdk.agents.version} is a qualified version`, () => {
  assert.ok(LIVEKIT_AGENTS_SUPPORTED_VERSIONS.includes(sdk.agents.version));
  assert.equal(typeof sdk.agents.JobContext.prototype._onSessionEnd, 'function');
  assert.equal(typeof sdk.agents.JobContext.prototype.makeSessionReport, 'function');
});

test('uploads the SDK-written audio.ogg once, byte-identical, after the recorder closes; SDK hook still runs', async t => {
  const a = await attachmentFixture(); t.after(() => a.close());
  const s = await startedSession(sdk, tracer); t.after(() => s.cleanup());
  const binding = bindLiveKitSessionRecording(s.session, s.ctx, { manager: a.manager, agents: sdk.agents, ownedRecordingFiles: true });
  assert.equal(binding.status, 'session_bound');
  assert.equal(a.requests.length, 0, 'binding opens nothing');
  await endSession(s);
  assert.equal(binding.status, 'validating');
  const file = fs.readFileSync(s.file);
  assert.equal(a.uploaded.length, 1); assert.ok(a.uploaded[0].equals(file));
  const request = a.requests[0];
  assert.equal(request.mime_type, 'audio/ogg'); assert.equal(request.direction, 'mixed');
  assert.equal(request.source_kind, 'OwnedRecordingFile'); assert.equal(request.purpose, 'conversation_recording');
  assert.equal(request.declared_bytes, file.length);
  assert.equal(request.association.parent.span_id, s.span.spanContext().spanId);
  assert.equal(request.association.parent.trace_id.replaceAll('-', ''), s.span.spanContext().traceId);
  assert.equal('channel' in request || 'role' in request || 'recording_id' in request, false);
  assert.deepEqual(s.session._recordedEvents, [], 'the SDK _onSessionEnd ran after the binding');
  assert.equal(Object.prototype.hasOwnProperty.call(s.ctx, '_onSessionEnd'), false, 'hook restored');
  assert.ok(fs.existsSync(s.file), 'the binding never deletes the recording');
  if (ffprobe) {
    const out = path.join(s.ctx.sessionDirectory, 'uploaded-copy.ogg'); fs.writeFileSync(out, a.uploaded[0]);
    const probe = execFileSync('ffprobe', ['-v', 'error', '-show_entries', 'stream=codec_name,channels', '-of', 'csv=p=0', out]).toString().trim();
    assert.match(probe, /^opus,[12]$/);
  }
});

test('a recorder still recording at finalization is source_unavailable; the SDK then closes it as before', async t => {
  const a = await attachmentFixture(); t.after(() => a.close());
  const s = await startedSession(sdk, tracer); t.after(() => s.cleanup());
  const binding = bindLiveKitSessionRecording(s.session, s.ctx, { manager: a.manager, agents: sdk.agents, ownedRecordingFiles: true });
  await endSession(s, { closeRecorder: false });
  assert.equal(binding.status, 'source_unavailable'); assert.equal(a.requests.length, 0);
  assert.equal(s.recorder.recording, false, 'SDK _onSessionEnd closed the recorder');
});

for (const [name, options, expected] of [
  ['ownedRecordingFiles false', { ownedRecordingFiles: false }, 'disabled'],
  ['unsupported agents version', { agents: { ...sdk.agents, version: '1.7.0' } }, 'unsupported_version'],
  ['foreign session object', { foreignSession: true }, 'unsupported_version'],
]) test(`${name} binds nothing (${expected})`, async t => {
  const a = await attachmentFixture(); t.after(() => a.close());
  const s = await startedSession(sdk, tracer, { record: false }); t.after(() => s.cleanup());
  const session = options.foreignSession ? {} : s.session;
  const binding = bindLiveKitSessionRecording(session, s.ctx, { manager: a.manager, agents: sdk.agents, ownedRecordingFiles: true, ...options });
  assert.equal(binding.status, expected);
  assert.equal(Object.prototype.hasOwnProperty.call(s.ctx, '_onSessionEnd'), false);
});

for (const [name, setup, expected, config, policy] of [
  ['attachments disabled', {}, 'disabled', { enabled: false }],
  ['environment kill switch', {}, 'disabled', { environment: () => ({ FI_ATTACHMENTS_ENABLED: 'false' }) }],
  ['core PII redaction', {}, 'redaction_unavailable', { traceConfig: { piiRedaction: true } }],
  ['core hidden input', {}, 'policy_denied', { traceConfig: { hideInputs: true } }],
  ['core hidden output', {}, 'policy_denied', { traceConfig: { hideOutputs: true } }],
  ['server requires media redaction', {}, 'redaction_unavailable', {}, { require_media_redaction: true }],
  ['server disallows owned files', {}, 'policy_denied', {}, { allowed_sources: ['Bytes'] }],
  ['LiveKit project redaction', { redaction: true }, 'redaction_unavailable'],
  ['session audio recording off', { audio: false }, 'policy_denied'],
]) test(`${name}: denied at bind (${expected}), no stat, no hook, no traffic`, async t => {
  const a = await attachmentFixture(config, policy); t.after(() => a.close());
  const s = await startedSession(sdk, tracer, setup); t.after(() => s.cleanup());
  const binding = bindLiveKitSessionRecording(s.session, s.ctx, { manager: a.manager, agents: sdk.agents, ownedRecordingFiles: true });
  assert.equal(binding.status, expected);
  assert.equal(Object.prototype.hasOwnProperty.call(s.ctx, '_onSessionEnd'), false);
  await endSession(s);
  await a.manager.flush();
  assert.equal(a.requests.length, 0); assert.equal(a.uploaded.length, 0);
});

test('LIVEKIT_TELEMETRY_ALLOW_PII=0 denies the recording (deny wins)', async t => {
  const a = await attachmentFixture(); t.after(() => a.close());
  const s = await startedSession(sdk, tracer); t.after(() => s.cleanup());
  const saved = process.env.LIVEKIT_TELEMETRY_ALLOW_PII; process.env.LIVEKIT_TELEMETRY_ALLOW_PII = '0';
  t.after(() => { if (saved === undefined) delete process.env.LIVEKIT_TELEMETRY_ALLOW_PII; else process.env.LIVEKIT_TELEMETRY_ALLOW_PII = saved; });
  const binding = bindLiveKitSessionRecording(s.session, s.ctx, { manager: a.manager, agents: sdk.agents, ownedRecordingFiles: true });
  assert.equal(binding.status, 'policy_denied');
  await endSession(s); assert.equal(a.requests.length, 0);
});

test('policy tightened after bind: finalization re-checks before any stat or open', async t => {
  const a = await attachmentFixture(); t.after(() => a.close());
  const s = await startedSession(sdk, tracer); t.after(() => s.cleanup());
  const binding = bindLiveKitSessionRecording(s.session, s.ctx, { manager: a.manager, agents: sdk.agents, ownedRecordingFiles: true });
  s.ctx._redactionEnabled = true; // e.g. the project mandated redaction mid-session
  await endSession(s);
  assert.equal(binding.status, 'redaction_unavailable'); assert.equal(a.requests.length, 0);
});

/**
 * Records every fs call on the session directory made while the binding is still finalizing
 * (status `session_bound`). The binding assigns its outcome before the SDK's own `_onSessionEnd`
 * runs, so the SDK's later work is excluded. Covers the binding's lstat and the core scope's
 * realpath/open (both go through the shared `node:fs` promises object).
 */
function watchSessionFs(t, s, binding) {
  const fsp = require('node:fs/promises');
  const seen = [];
  const inDir = p => { try { return String(p).startsWith(s.ctx.sessionDirectory); } catch { return false; } };
  const wrap = (target, names) => {
    for (const name of names) {
      const original = target[name];
      if (typeof original !== 'function') continue;
      target[name] = function (p, ...rest) {
        if (binding.status === 'session_bound' && inDir(p)) seen.push(`${name}:${path.relative(s.ctx.sessionDirectory, String(p)) || '.'}`);
        return original.call(this, p, ...rest);
      };
      t.after(() => { target[name] = original; });
    }
  };
  wrap(fsp, ['lstat', 'stat', 'open', 'realpath', 'readFile', 'readdir', 'opendir', 'access']);
  wrap(fs, ['lstatSync', 'statSync', 'openSync', 'realpathSync', 'readFileSync', 'readdirSync', 'accessSync', 'createReadStream']);
  return seen;
}

test('control: an admitted session end does stat and open audio.ogg (the fs watch sees the binding)', async t => {
  const a = await attachmentFixture(); t.after(() => a.close());
  const s = await startedSession(sdk, tracer); t.after(() => s.cleanup());
  const binding = bindLiveKitSessionRecording(s.session, s.ctx, { manager: a.manager, agents: sdk.agents, ownedRecordingFiles: true });
  const seen = watchSessionFs(t, s, binding);
  await endSession(s);
  assert.equal(binding.status, 'validating');
  assert.ok(seen.includes('lstat:audio.ogg'), `seen: ${seen.join(',')}`);
  assert.ok(seen.includes('open:audio.ogg'), `seen: ${seen.join(',')}`);
  assert.equal(a.uploaded.length, 1);
});

for (const [name, tighten, expected] of [
  ['manager PII redaction enabled', a => a.manager.updatePolicy({ traceConfig: { piiRedaction: true } }), 'redaction_unavailable'],
  ['manager output hidden', a => a.manager.updatePolicy({ traceConfig: { hideOutputs: true } }), 'policy_denied'],
  ['manager disabled', a => a.manager.updatePolicy({ enabled: false }), 'disabled'],
  ['environment kill switch flipped', (a, env) => { env.FI_ATTACHMENTS_ENABLED = 'false'; }, 'disabled'],
  ['manager invalidated (revoked)', a => a.manager.invalidate('policy_denied'), 'policy_denied'],
]) {
  for (const removeFile of [false, true]) {
    test(`session-end deny before any stat or open: ${name}${removeFile ? ', recording removed' : ''} -> ${expected}`, async t => {
      const env = {};
      const a = await attachmentFixture({ environment: () => env }); t.after(() => a.close());
      const s = await startedSession(sdk, tracer); t.after(() => s.cleanup());
      const binding = bindLiveKitSessionRecording(s.session, s.ctx, { manager: a.manager, agents: sdk.agents, ownedRecordingFiles: true });
      assert.equal(binding.status, 'session_bound', 'admitted at bind');
      await s.recorder.close();
      tighten(a, env);
      // A removed file would make a stat-first order report source_unavailable instead of the policy reason.
      if (removeFile) fs.rmSync(s.file);
      // Watch only the session end; the test's own recorder close and rmSync (which lstats) stay outside.
      const seen = watchSessionFs(t, s, binding);
      await endSession(s, { closeRecorder: false });
      assert.equal(binding.status, expected);
      assert.deepEqual(seen, [], 'no stat, open or realpath of the session directory before the deny');
      await a.manager.flush().catch(() => undefined);
      assert.equal(a.requests.length, 0); assert.equal(a.uploaded.length, 0);
      assert.deepEqual(s.session._recordedEvents, [], 'the SDK _onSessionEnd still ran');
    });
  }
}

test('a recording file owned by another user is rejected (source_unavailable), nothing uploaded', async t => {
  // Only root can hand a file to another uid; CI and the qualification VM run this suite as root.
  if (typeof process.getuid !== 'function' || process.getuid() !== 0) {
    t.diagnostic('SKIPPED: needs uid 0 to chown the recording to another user');
    t.skip('needs uid 0'); return;
  }
  const a = await attachmentFixture(); t.after(() => a.close());
  const s = await startedSession(sdk, tracer); t.after(() => s.cleanup());
  const binding = bindLiveKitSessionRecording(s.session, s.ctx, { manager: a.manager, agents: sdk.agents, ownedRecordingFiles: true });
  assert.equal(binding.status, 'session_bound');
  await s.recorder.close();
  fs.chownSync(s.file, 65534, 65534);
  const info = fs.lstatSync(s.file);
  assert.ok(info.isFile() && info.nlink === 1 && info.uid === 65534, 'only the owner differs');
  await endSession(s, { closeRecorder: false });
  assert.equal(binding.status, 'source_unavailable');
  await a.manager.flush(); assert.equal(a.requests.length, 0); assert.equal(a.uploaded.length, 0);
  assert.ok(fs.existsSync(s.file), 'the binding never deletes the recording');
});

for (const [name, mutate] of [
  ['report absent (no recorder)', s => { s.session._recorderIO = undefined; }],
  ['recording replaced by a symlink', s => { const other = path.join(s.ctx.sessionDirectory, 'other.ogg'); fs.renameSync(s.file, other); fs.symlinkSync(other, s.file); }],
  ['recording hard-linked elsewhere', s => { fs.linkSync(s.file, path.join(s.ctx.sessionDirectory, 'alias.ogg')); }],
  ['recording deleted by the application', s => { fs.rmSync(s.file); }],
  ['recorder path outside the session directory', s => { Object.defineProperty(s.session._recorderIO, 'outputPath', { value: '/tmp/elsewhere/audio.ogg' }); }],
  ['another primary session', s => { s.ctx._primaryAgentSession = {}; }],
]) test(`${name}: source_unavailable, nothing uploaded`, async t => {
  const a = await attachmentFixture(); t.after(() => a.close());
  const s = await startedSession(sdk, tracer); t.after(() => s.cleanup());
  const binding = bindLiveKitSessionRecording(s.session, s.ctx, { manager: a.manager, agents: sdk.agents, ownedRecordingFiles: true });
  assert.equal(binding.status, 'session_bound');
  await s.recorder.close();
  mutate(s);
  s.span.end();
  try { await s.ctx._onSessionEnd(); } catch { /* the SDK's own hook may reject on a broken session */ }
  assert.equal(binding.status, 'source_unavailable');
  await a.manager.flush(); assert.equal(a.uploaded.length, 0);
});

test('T-LS-05: the file swapped after identify and before transfer fails closed', async t => {
  const a = await attachmentFixture({ workers: 1 }); t.after(() => a.close());
  const s = await startedSession(sdk, tracer); t.after(() => s.cleanup());
  const binding = bindLiveKitSessionRecording(s.session, s.ctx, { manager: a.manager, agents: sdk.agents, ownedRecordingFiles: true });
  // Occupy the single worker so the owned-file job waits in the queue after identify.
  const release = a.holdNext();
  const blocker = a.manager.attach(s.span.spanContext(), { kind: 'Bytes', data: new Uint8Array([1, 2, 3]) }, { mimeType: 'audio/wav', direction: 'output' });
  assert.equal(blocker.admission, 'queued');
  await s.recorder.close(); s.span.end();
  const ending = endSession(s, { closeRecorder: false });
  while (!binding.recording) await new Promise(r => setTimeout(r, 5));
  const original = fs.readFileSync(s.file);
  const swap = path.join(s.ctx.sessionDirectory, 'swap.ogg');
  fs.writeFileSync(swap, Buffer.alloc(original.length, 7)); fs.renameSync(swap, s.file);
  release();
  await ending;
  assert.equal(binding.status, 'source_unavailable');
  assert.equal(a.uploaded.filter(b => b.length === original.length).length, 0, 'swapped bytes never uploaded');
});

test('deadline: a stalled transfer ends incomplete within the bound and the SDK hook still runs', async t => {
  const a = await attachmentFixture(); t.after(() => a.close());
  const s = await startedSession(sdk, tracer); t.after(() => s.cleanup());
  const binding = bindLiveKitSessionRecording(s.session, s.ctx, { manager: a.manager, agents: sdk.agents, ownedRecordingFiles: true, finalizationTimeoutMs: 300 });
  const release = a.holdNext(); t.after(release);
  const started = Date.now();
  await endSession(s);
  const elapsed = Date.now() - started;
  assert.equal(binding.status, 'incomplete');
  assert.ok(elapsed < 3000, `finalization took ${elapsed} ms`);
  assert.deepEqual(s.session._recordedEvents, []);
});

test('bind is idempotent for identical options and reports conflict otherwise; close() restores the hook', async t => {
  const a = await attachmentFixture(); t.after(() => a.close());
  const s = await startedSession(sdk, tracer); t.after(() => s.cleanup());
  const options = { manager: a.manager, agents: sdk.agents, ownedRecordingFiles: true };
  const first = bindLiveKitSessionRecording(s.session, s.ctx, options);
  assert.equal(bindLiveKitSessionRecording(s.session, s.ctx, options), first);
  assert.equal(bindLiveKitSessionRecording(s.session, s.ctx, { ...options, finalizationTimeoutMs: 1000 }).status, 'conflict');
  assert.ok(first instanceof LiveKitSessionRecording);
  first.close();
  assert.equal(Object.prototype.hasOwnProperty.call(s.ctx, '_onSessionEnd'), false);
  await endSession(s); assert.equal(a.requests.length, 0);
});

test('invalid options throw synchronously and never touch the session', () => {
  assert.throws(() => bindLiveKitSessionRecording({}, {}, { manager: {}, agents: sdk.agents }), TypeError);
  assert.throws(() => bindLiveKitSessionRecording({}, {}, { manager: {}, agents: sdk.agents, ownedRecordingFiles: true, finalizationTimeoutMs: 30001 }), RangeError);
  assert.throws(() => bindLiveKitSessionRecording({}, {}, { manager: {}, agents: sdk.agents, ownedRecordingFiles: true, capture: { newRecording: true } }), TypeError);
});

test('a recording whose owner is not the worker uid is rejected (any uid; worker uid reported differently)', async t => {
  // Runs without root too: the file keeps its real owner and the worker reports another uid.
  const a = await attachmentFixture(); t.after(() => a.close());
  const s = await startedSession(sdk, tracer); t.after(() => s.cleanup());
  const binding = bindLiveKitSessionRecording(s.session, s.ctx, { manager: a.manager, agents: sdk.agents, ownedRecordingFiles: true });
  assert.equal(binding.status, 'session_bound');
  await s.recorder.close(); s.span.end();
  const real = process.getuid;
  process.getuid = () => real.call(process) + 1;
  let status;
  try { status = await binding.finalize(); } finally { process.getuid = real; }
  assert.equal(status, 'source_unavailable');
  await a.manager.flush(); assert.equal(a.requests.length, 0); assert.equal(a.uploaded.length, 0);
  assert.ok(fs.existsSync(s.file), 'the binding never deletes the recording');
  await s.ctx._onSessionEnd();
});
