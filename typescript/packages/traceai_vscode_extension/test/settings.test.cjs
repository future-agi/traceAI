const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { plan, removal, parse, save, readSettings, synchronize, readInstallation } = require('../dist');
const base = '// keep top comment\n{\n  "editor.fontSize": 15, // keep inline\n  "files.exclude": {"**/.git":true,},\n  "github.copilot.chat.otel.enabled": false,\n  "settingsSync.ignoredSettings": ["editor.fontSize", "-chat.agentHost.otel.otlpEndpoint"],\n}\n';
const token = 'a'.repeat(64);
test('both harnesses use correct endpoint semantics without keys and preserve JSONC', () => {
  const p = plan(base, 45678, token, true);
  const json = parse(p.text);
  assert.equal(json['github.copilot.chat.otel.otlpEndpoint'], `http://127.0.0.1:45678/${token}`);
  assert.equal(json['chat.agentHost.otel.otlpEndpoint'], `http://127.0.0.1:45678/${token}/v1/traces`);
  assert.match(p.text, /keep top comment/); assert.match(p.text, /keep inline/);
  assert.equal(json['editor.fontSize'], 15); assert.deepEqual({ ...json['files.exclude'] }, { '**/.git': true });
  assert.doesNotMatch(p.text, /X-Api-Key|X-Secret-Key|synthetic/);
  for (const k of Object.keys(p.ownership.values)) assert.ok(json['settingsSync.ignoredSettings'].includes(k));
  assert.ok(!json['settingsSync.ignoredSettings'].includes('-chat.agentHost.otel.otlpEndpoint'));
  assert.equal(plan(p.text, 45678, token, true, p.ownership).text, p.text);
  assert.deepEqual(parse(removal(p.text, p.ownership).text), parse(base));
});
test('privacy update is owned and uninstall preserves new unrelated edits', () => {
  const p = plan(base, 45678, token, true), off = plan(p.text, 45678, token, false, p.ownership);
  assert.equal(parse(off.text)['chat.agentHost.otel.captureContent'], false);
  const edited = off.text.replace('"editor.fontSize": 15', '"editor.fontSize": 20');
  const restored = removal(edited, off.ownership);
  assert.equal(parse(restored.text)['editor.fontSize'], 20);
  assert.equal(parse(restored.text)['github.copilot.chat.otel.enabled'], false);
  assert.equal(restored.conflicts.length, 0);
});
test('user edits to managed keys are never overwritten or deleted', () => {
  const p = plan(base, 45678, token, true), changed = p.text.replace(`http://127.0.0.1:45678/${token}/v1/traces`, 'http://127.0.0.1:5555/v1/traces');
  assert.throws(() => plan(changed, 45678, token, true, p.ownership), /OWNED_SETTING_CHANGED/);
  const r = removal(changed, p.ownership);
  assert.deepEqual(r.conflicts, ['chat.agentHost.otel.otlpEndpoint']);
  assert.equal(parse(r.text)['chat.agentHost.otel.otlpEndpoint'], 'http://127.0.0.1:5555/v1/traces');
  assert.ok(parse(r.text)['settingsSync.ignoredSettings'].includes('chat.agentHost.otel.otlpEndpoint'));
});
test('rejects ambiguous settings, active exporters, headers and telemetry opt-out', () => {
  for (const text of ['{"a":1,"a":2}', '[]', '{broken']) assert.throws(() => plan(text, 45678, token, true));
  for (const obj of [ { 'telemetry.telemetryLevel': 'off' }, { 'github.copilot.chat.otel.enabled': true },
    { 'github.copilot.chat.otel.headers': { Authorization: 'synthetic' } }, { 'chat.agentHost.otel.dbSpanExporter.enabled': true }]) {
    assert.throws(() => plan(JSON.stringify(obj), 45678, token, true));
  }
  const p = plan(base, 45678, token, true);
  assert.throws(() => removal(p.text, { ...p.ownership, values: { ...p.ownership.values, 'editor.fontSize': 15 } }), /INVALID_OWNERSHIP/);
});
test('sync restores original ordering and rejects a later opt-in of sensitive endpoints', () => {
  const original = JSON.stringify({ 'settingsSync.ignoredSettings': ['first', '-chat.agentHost.otel.otlpEndpoint', 'last'] });
  const p = plan(original, 45678, token, true);
  assert.deepEqual(parse(removal(p.text, p.ownership).text)['settingsSync.ignoredSettings'], ['first', '-chat.agentHost.otel.otlpEndpoint', 'last']);
  const json = parse(p.text); json['settingsSync.ignoredSettings'].push('-github.copilot.chat.otel.otlpEndpoint');
  assert.throws(() => plan(JSON.stringify(json), 45678, token, true, p.ownership), /SYNC_PROTECTION_CHANGED/);
  assert.equal(json['chat.agentHost.otel.headers'], undefined);
});
test('atomic protected writer preserves stale files and rejects symlink targets', async t => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'vscode-writer-'));
  t.after(() => fs.rm(home, { recursive:true, force:true }));
  const file = path.join(home, 'settings.json');
  await fs.writeFile(file, base, { mode: 0o644 });
  const p = plan(base, 45678, token, true);
  await save(file, base, p.text); assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
  await assert.rejects(save(file, base, p.text), /CONCURRENT_SAVE/);
  const link = path.join(home, 'link.json'); await fs.symlink(file, link);
  await assert.rejects(readSettings(link));
});
test('enable/disable restore ownership and are idempotent across process restarts', async t => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'vscode-sync-'));
  t.after(() => fs.rm(home, {recursive:true, force:true}));
  const file = path.join(home, 'relay.json'), settings = path.join(home, 'User/settings.json');
  await fs.mkdir(path.dirname(settings)); await fs.writeFile(settings, base);
  await fs.writeFile(file, JSON.stringify({ version:1, id:'b'.repeat(32), token, port:45678, settingsPath: settings, scope:'user', channel:'stable', cli:'/synthetic/code', extensionOwned:true, applied:false, captureContent:true }), {mode:0o600});
  await synchronize(file, true);
  assert.equal((await readInstallation(file)).applied, true);
  await synchronize(file, false); await synchronize(file, false);
  assert.deepEqual(parse(await fs.readFile(settings, 'utf8')), parse(base));
  await synchronize(file, true, false);
  assert.equal(parse(await fs.readFile(settings, 'utf8'))['chat.agentHost.otel.captureContent'], false);
  await synchronize(file, false);
  await fs.unlink(settings);
  await synchronize(file, true);
  await synchronize(file, false);
  await assert.rejects(fs.stat(settings), { code: 'ENOENT' });
});
