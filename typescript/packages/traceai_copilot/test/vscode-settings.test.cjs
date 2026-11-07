const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const settings = require('../dist/vscode/settings.js');
const { discoverSettings, inspectSettings, inspectEditor } = require('../dist/vscode/discovery.js');
const { createVscodeAdapter } = require('../dist/vscode/index.js');
const { configPaths, inspectStatus, disable, resolveConfig } = require('@traceai/agent-core');
const endpoint = 'http://127.0.0.1:43189';
const base = '// keep my comment\n{\n  "editor.fontSize": 14, // user preference\n  "files.exclude": { "**/.git": true, },\n}\n';
async function fixture(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'copilot-vscode-'));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const env = { FI_AGENT_CONFIG_DIR: path.join(home, 'config'), FI_COPILOT_VSCODE_HARNESS: 'legacy', XDG_STATE_HOME: path.join(home, 'state') };
  return { home, env };
}
for (const harness of ['agent-host', 'legacy']) {
  test(`${harness}: comment-preserving merge is idempotent and owns only its keys`, () => {
    const plan = settings.planSettings(base, harness, endpoint, true);
    const parsed = settings.parseSettings(plan.text);
    const prefix = settings.prefixes[harness];
    assert.equal(parsed[prefix + 'captureContent'], true);
    assert.equal(parsed[prefix + 'enabled'], true);
    assert.equal(parsed[prefix + 'otlpEndpoint'], endpoint);
    assert.equal(parsed['editor.fontSize'], 14);
    assert.match(plan.text, /keep my comment/);
    assert.match(plan.text, /user preference/);
    assert.deepEqual(Object.keys(plan.ownership.values).sort(), settings.ownedSuffixes.map(s => prefix + s).sort());
    assert.equal(settings.planSettings(plan.text, harness, endpoint, true, plan.ownership).text, plan.text);
    const out = settings.planSettings(plan.text, harness, endpoint, false, plan.ownership);
    assert.equal(settings.parseSettings(out.text)[prefix + 'captureContent'], false);
    const removed = settings.planRemoval(out.text, out.ownership);
    assert.deepEqual(settings.parseSettings(removed.text), settings.parseSettings(base));
    assert.deepEqual(removed.conflicts, []);
    assert.match(removed.text, /keep my comment/);
    assert.ok(!/headers|X-Api-Key|X-Secret-Key|OTEL_/.test(plan.text));
  });
}
test('does not steal an existing setting, even when the value matches', () => {
  assert.throws(() => settings.planSettings('{"github.copilot.chat.otel.enabled":true}', 'legacy', endpoint, true), /EXISTING_TELEMETRY/);
  assert.throws(() => settings.planSettings('{"github.copilot.chat.otel.enabled":false}', 'legacy', endpoint, true), /EXISTING_TELEMETRY/);
});
test('no competing harness, file/DB fanout, user header overwrite or telemetry consent bypass', () => {
  for (const current of [
    { 'chat.agentHost.otel.enabled': true },
    { 'github.copilot.chat.otel.dbSpanExporter.enabled': true },
    { 'github.copilot.chat.otel.headers': { Authorization: 'synthetic-do-not-print' } },
    { 'github.copilot.chat.otel.outfile': '/synthetic/path' },
    { 'github.copilot.chat.otel.captureIdentity': true },
  ]) assert.throws(() => settings.planSettings(JSON.stringify(current), 'legacy', endpoint, true), /OTHER_TELEMETRY/);
  assert.throws(() => settings.planSettings('{"telemetry.telemetryLevel":"off"}', 'legacy', endpoint, true), /TELEMETRY_DISABLED/);
});
test('changed owned settings are not overwritten or removed', () => {
  const plan = settings.planSettings(base, 'legacy', endpoint, true);
  const changed = plan.text.replace(endpoint, 'http://127.0.0.1:5555');
  assert.throws(() => settings.planSettings(changed, 'legacy', endpoint, true, plan.ownership), /OWNED_SETTING_CHANGED/);
  const removal = settings.planRemoval(changed, plan.ownership);
  assert.deepEqual(removal.conflicts, ['github.copilot.chat.otel.otlpEndpoint']);
  assert.equal(settings.parseSettings(removal.text)['github.copilot.chat.otel.otlpEndpoint'], 'http://127.0.0.1:5555');
  assert.equal(settings.parseSettings(removal.text)['editor.fontSize'], 14);
});
test('ownership cannot reach arbitrary settings or another harness', () => {
  const plan = settings.planSettings(base, 'legacy', endpoint, true);
  assert.throws(() => settings.planSettings(plan.text, 'agent-host', endpoint, true, plan.ownership), /OWNERSHIP_MISMATCH/);
  assert.throws(() => settings.planRemoval(plan.text, { ...plan.ownership, values: { ...plan.ownership.values, 'editor.fontSize': 14 } }), /INVALID_OWNERSHIP/);
});
test('malformed, ambiguous or oversized JSONC is rejected without exposing content', () => {
  for (const text of ['[]', 'null', '{"x":', '{"x":1,"x":2}', '{"nested":{"x":1,"x":2}}', '{"secret":"SYNTHETIC_PRIVATE" bad}']) {
    assert.throws(() => settings.parseSettings(text), error => !error.message.includes('SYNTHETIC_PRIVATE'));
  }
  assert.throws(() => settings.parseSettings(' '.repeat(65537)), /TOO_LARGE/);
  assert.equal(settings.parseSettings('{"example":"// not a comment",}').example, '// not a comment');
});
test('endpoint never contains credentials, a remote host or a signal path', () => {
  for (const url of ['https://api.futureagi.com/tracer', 'http://localhost:1234', 'http://0.0.0.0:1234', 'http://127.0.0.1',
    'http://key:secret@127.0.0.1:1234', endpoint + '/v1/traces', endpoint + '?key=x', endpoint + '#x']) {
    assert.throws(() => settings.localEndpoint(url), /LOOPBACK_ENDPOINT_REQUIRED/);
  }
});
test('protected write handles ordinary 0644 settings and stale plans', async (t) => {
  const { home } = await fixture(t);
  const file = path.join(home, 'settings.json');
  await fs.writeFile(file, base, { mode: 0o644 });
  const plan = settings.planSettings(await settings.readSettingsFile(file), 'legacy', endpoint, true);
  await settings.writeSettingsPlan(file, plan);
  assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
  await fs.writeFile(file, base + '\n');
  await assert.rejects(settings.writeSettingsPlan(file, plan), /CONCURRENT_SAVE/);
  assert.equal(await fs.readFile(file, 'utf8'), base + '\n');
});
test('shared-writable settings are rejected without replacing them', async (t) => {
  const { home } = await fixture(t);
  const file = path.join(home, 'shared.json');
  await fs.writeFile(file, base);
  await fs.chmod(file, 0o666);
  await assert.rejects(settings.readSettingsFile(file), /UNSAFE_FILE/);
  await assert.rejects(settings.writeSettingsPlan(file, settings.planSettings(base, 'legacy', endpoint, true)), /UNSAFE_FILE/);
  assert.equal(await fs.readFile(file, 'utf8'), base);
});
test('protected settings operations refuse symlink and hardlink targets', async (t) => {
  const { home } = await fixture(t);
  const real = path.join(home, 'real.json'), linked = path.join(home, 'link.json');
  await fs.writeFile(real, base, { mode: 0o600 });
  await fs.symlink(real, linked);
  await assert.rejects(settings.readSettingsFile(linked));
  await assert.rejects(settings.writeSettingsPlan(linked, settings.planSettings(base, 'legacy', endpoint, true)));
  await fs.unlink(linked); await fs.link(real, linked);
  await assert.rejects(settings.readSettingsFile(linked), /UNSAFE_FILE/);
  assert.equal(await fs.readFile(real, 'utf8'), base);
});
test('discovery selects the actual editor profile, never COPILOT_HOME', () => {
  const mac = discoverSettings({ home: '/synthetic/home', platform: 'darwin', env: { COPILOT_HOME: '/wrong' } });
  assert.equal(mac.userSettings, '/synthetic/home/Library/Application Support/Code/User/settings.json');
  assert.equal(mac.selectedHarness, undefined);
  const linux = discoverSettings({ home: '/synthetic/home', platform: 'linux', env: { XDG_CONFIG_HOME: '/custom', FI_COPILOT_VSCODE_CHANNEL: 'insiders' } });
  assert.equal(linux.userSettings, '/custom/Code - Insiders/User/settings.json');
  const portable = discoverSettings({ home: '/h', platform: 'linux', env: { VSCODE_PORTABLE: '/portable', FI_COPILOT_VSCODE_PROFILE_ID: 'profile-1', FI_COPILOT_VSCODE_HARNESS: 'agent-host' } });
  assert.equal(portable.userSettings, '/portable/user-data/User/profiles/profile-1/settings.json');
  assert.equal(portable.selectedHarness, 'agent-host');
  const custom = discoverSettings({ home: '/h', platform: 'linux', env: { FI_COPILOT_VSCODE_USER_DATA_DIR: '/custom-data' }, projectDir: '/project' });
  assert.equal(custom.userSettings, '/custom-data/User/settings.json');
  assert.equal(custom.workspaceSettings, '/project/.vscode/settings.json');
});
test('discovery refuses unqualified platforms, remote surfaces and unsafe profile overrides', () => {
  assert.throws(() => discoverSettings({ platform: 'win32' }), /PLATFORM_NOT_QUALIFIED/);
  for (const env of [{ SSH_CONNECTION: 'x' }, { VSCODE_AGENT_FOLDER: '/remote' }, { FI_COPILOT_VSCODE_PROFILE_ID: '../../escape' },
    { FI_COPILOT_VSCODE_USER_DATA_DIR: 'relative' }, { FI_COPILOT_VSCODE_HARNESS: 'claude' }, { FI_COPILOT_VSCODE_CHANNEL: 'unknown' }]) {
    assert.throws(() => discoverSettings({ platform: 'linux', env }));
  }
});
test('inspection reports file provenance, not effective GUI policy, and never leaks values', () => {
  const user = JSON.stringify({ 'github.copilot.chat.otel.enabled': true, 'github.copilot.chat.otel.headers': { 'X-Secret-Key': 'SYNTHETIC_PRIVATE' }, 'chat.agentHost.otel.enabled': true });
  const workspace = JSON.stringify({ 'github.copilot.chat.otel.captureContent': false, 'github.copilot.chat.otel.otlpEndpoint': 'https://PRIVATE_DESTINATION.example/' });
  const info = inspectSettings(user, workspace, 'legacy', { OTEL_EXPORTER_OTLP_HEADERS: 'SYNTHETIC_ENV_PRIVATE' });
  assert.equal(info.provenance['github.copilot.chat.otel.enabled'], 'user');
  assert.equal(info.provenance['github.copilot.chat.otel.captureContent'], 'workspace');
  assert.equal(info.qualified, false); assert.equal(info.activated, false); assert.equal(info.registered, false);
  assert.ok(info.issues.includes('VSCODE_MULTIPLE_TELEMETRY_HARNESSES'));
  assert.ok(info.issues.includes('VSCODE_WORKSPACE_SETTINGS_REQUIRE_EDITOR_RESOLUTION'));
  assert.deepEqual(info.environmentOverrides, ['OTEL_EXPORTER_OTLP_HEADERS']);
  assert.ok(!/SYNTHETIC|PRIVATE_DESTINATION/.test(JSON.stringify(info)));
});
test('two workspaces remain independent and reload rereads settings without caching', async (t) => {
  const f = await fixture(t);
  const location = discoverSettings({ ...f, platform: 'linux' });
  await fs.mkdir(path.dirname(location.userSettings), { recursive: true, mode: 0o700 });
  await fs.writeFile(location.userSettings, base, { mode: 0o644 });
  const a = path.join(f.home, 'a'), b = path.join(f.home, 'b');
  for (const root of [a, b]) await fs.mkdir(path.join(root, '.vscode'), { recursive: true, mode: 0o700 });
  await fs.writeFile(path.join(a, '.vscode/settings.json'), '{"github.copilot.chat.otel.enabled":true}');
  await fs.writeFile(path.join(b, '.vscode/settings.json'), '{}');
  const ia = await inspectEditor({ ...f, platform: 'linux', projectDir: a });
  const ib = await inspectEditor({ ...f, platform: 'linux', projectDir: b });
  assert.equal(ia.provenance['github.copilot.chat.otel.enabled'], 'workspace');
  assert.equal(ib.provenance['github.copilot.chat.otel.enabled'], undefined);
  await fs.writeFile(path.join(a, '.vscode/settings.json'), '{}');
  assert.equal((await inspectEditor({ ...f, platform: 'linux', projectDir: a })).provenance['github.copilot.chat.otel.enabled'], undefined);
});
test('editor config and kill switch do not share CLI profile files', async (t) => {
  const f = await fixture(t);
  const editor = await configPaths({ ...f, agent: 'copilot-vscode' });
  const cli = await configPaths({ ...f, agent: 'copilot' });
  assert.notEqual(editor.user, cli.user);
  assert.equal(editor.user, path.join(f.env.FI_AGENT_CONFIG_DIR, 'hosts/copilot-vscode/futureagi.json'));
  await disable({ ...f, agent: 'copilot-vscode' });
  assert.equal((await resolveConfig({ ...f, agent: 'copilot-vscode' })).enabled, false);
});
test('missing host fails detection without touching user settings; qualification remains separate', async (t) => {
  const f = await fixture(t);
  const location = discoverSettings({ ...f, platform: 'linux' });
  await fs.mkdir(path.dirname(location.userSettings), { recursive: true, mode: 0o700 });
  await fs.writeFile(location.userSettings, base, { mode: 0o644 });
  const adapter = createVscodeAdapter({ ...f, platform: 'linux' });
  const config = await resolveConfig({ ...f, agent: 'copilot-vscode' });
  const context = { config, scope: 'user' };
  assert.equal((await adapter.detect(context)).supported, false);
  await assert.rejects(adapter.install(context), /VSCODE_CLI_NOT_FOUND/);
  assert.equal((await adapter.inspect(context)).registered, false);
  assert.equal((await adapter.uninstall(context)).registered, false);
  assert.equal(await fs.readFile(location.userSettings, 'utf8'), base);
  assert.deepEqual(settings.qualifiedEditorVersions, []);
});
