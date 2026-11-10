const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { spawn } = require('node:child_process');
const { zipSync, strToU8 } = require('fflate');
const modules = process.env.FI_PACKED_MODULES;
const packageRoot = modules ? path.join(modules, '@traceai/augment-plugin') : path.resolve(__dirname, '..');
const core = require(modules ? path.join(modules, '@traceai/agent-core') : '@traceai/agent-core');
const { discoverAugmentEditors, ideRegistrationProfile, augmentIdeAdapter, IDE_HOLD } = require(path.join(packageRoot, 'dist/ide.js'));
const { augmentHookRegistration } = require(path.join(packageRoot, 'dist/adapter.js'));

async function homeFixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'fi-augment-ide-'));
  const home = path.join(root, 'home'), projectDir = path.join(root, 'project');
  await fs.mkdir(home, { mode: 0o700 }); await fs.mkdir(projectDir, { mode: 0o700 });
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  return { root, home, projectDir };
}
async function write(file, data) { await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 }); await fs.writeFile(file, data, { mode: 0o600 }); }
const vscode = (home) => path.join(home, '.vscode/extensions/augment.vscode-augment-0.904.1/package.json');
const xml = (id = 'com.augmentcode', version = '0.491.0-stable') => `<idea-plugin><id>${id}</id><version>${version}</version></idea-plugin>`;
const jar = (home, platform = 'linux') => path.join(home, ...(platform === 'darwin' ? ['Library', 'Application Support'] : ['.local', 'share']), 'JetBrains/IntelliJIdea2026.2/plugins/augment-intellij/lib/augment.jar');
const vsManifest = JSON.stringify({ name: 'vscode-augment', publisher: 'augment', version: '0.904.1' });
const zip = (value) => zipSync({ 'META-INF/plugin.xml': strToU8(value), 'do-not-load.js': strToU8('throw new Error("executed")') });
async function fixture(t) {
  const f = await homeFixture(t), received = [];
  const server = http.createServer((req, res) => {
    let data = ''; req.on('data', (b) => data += b); req.on('end', () => {
      received.push(JSON.parse(data)); res.writeHead(200, { 'content-type': 'application/json' }); res.end('{}');
    });
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  t.after(() => new Promise((r) => server.close(r)));
  const env = { HOME: f.home, XDG_STATE_HOME: path.join(f.home, '.state'), XDG_CONFIG_HOME: path.join(f.home, '.config'),
    FI_API_KEY: 'synthetic-api', FI_SECRET_KEY: 'synthetic-secret', FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/tracer/v1/traces` };
  const options = { agent: 'augment-ide', home: f.home, projectDir: f.projectDir, env };
  await core.setup({ ...options, projectName: 'editor-test', configureOnly: true });
  const config = await core.resolveConfig(options);
  return { ...f, options, env, config, context: { config, projectDir: f.projectDir, scope: 'user' }, received,
    spans: () => received.flatMap((b) => b.resourceSpans ?? []).flatMap((r) => r.scopeSpans ?? []).flatMap((r) => r.spans ?? []) };
}
async function run(file, args, env, input = '{}', cwd) {
  return new Promise((resolve, reject) => {
    const child = spawn(file, args, { env, cwd, stdio: ['pipe', 'pipe', 'pipe'] }); let stdout = '', stderr = '';
    child.on('error', reject); child.stdout.on('data', (b) => stdout += b); child.stderr.on('data', (b) => stderr += b);
    child.on('close', (code) => resolve({ code, stdout, stderr })); child.stdin.end(input);
  });
}
const attrs = (s) => Object.fromEntries(s.attributes.map((a) => [a.key, a.value.stringValue ?? a.value.intValue]));

test('empty home means no editor installations, not missing CLI', async (t) => {
  const f = await homeFixture(t);
  assert.deepEqual(await discoverAugmentEditors({ home: f.home, env: {}, platform: 'linux' }), { installations: [], issues: [] });
});
for (const platform of ['linux', 'darwin']) test(`separate ${platform} editor manifest discovery is not activation evidence`, async (t) => {
  const f = await homeFixture(t); await write(vscode(f.home), vsManifest); await write(jar(f.home, platform), zip(xml()));
  const found = await discoverAugmentEditors({ home: f.home, env: {}, platform });
  assert.equal(found.installations.length, 2); assert.deepEqual(found.issues, []);
  assert.deepEqual(found.installations.map((i) => [i.editor, i.pluginId, i.version]), [
    ['vscode', 'augment.vscode-augment', '0.904.1'], ['intellij', 'com.augmentcode', '0.491.0-stable'],
  ]);
  assert.ok(found.installations.every((i) => !('activated' in i)));
});
test('explicit plugin directories and Linux XDG data are respected', async (t) => {
  const f = await homeFixture(t), root = path.join(f.home, 'custom'), ext = path.join(root, 'extensions');
  await write(path.join(ext, 'augment.vscode-augment-0.904.1/package.json'), vsManifest);
  await write(path.join(root, 'JetBrains/IdeaIC2025.3/plugins/augment/META-INF/plugin.xml'), xml());
  const found = await discoverAugmentEditors({ home: f.home, env: { XDG_DATA_HOME: root }, platform: 'linux', vscodeExtensionsDir: ext });
  assert.equal(found.installations.length, 2);
  await assert.rejects(discoverAugmentEditors({ home: f.home, intellijPluginsDirs: ['relative'] }), /ABSOLUTE/);
});
test('wrong publisher, nightly and other JetBrains plugins never count as Augment', async (t) => {
  const f = await homeFixture(t);
  await write(vscode(f.home), vsManifest.replace('"publisher":"augment"', '"publisher":"not-augment"'));
  await write(path.join(f.home, '.vscode/extensions/augment.vscode-augment-nightly-0.904.1/package.json'), vsManifest);
  await write(jar(f.home), zip(xml('not.augment')));
  assert.equal((await discoverAugmentEditors({ home: f.home, env: {} })).installations.length, 0);
});
test('symlink metadata, corrupt JAR and oversize manifests are reported without reading adjacent files', async (t) => {
  const f = await homeFixture(t);
  await write(vscode(f.home), vsManifest); await fs.unlink(vscode(f.home));
  await write(path.join(f.root, 'outside.json'), vsManifest); await fs.symlink(path.join(f.root, 'outside.json'), vscode(f.home));
  await write(jar(f.home), 'not a ZIP');
  const found = await discoverAugmentEditors({ home: f.home, env: {} });
  assert.deepEqual(found.issues, ['VSCODE_DISCOVERY_INCOMPLETE', 'INTELLIJ_DISCOVERY_INCOMPLETE']);
  await fs.unlink(vscode(f.home)); await write(vscode(f.home), 'x'.repeat(1024 * 1024 + 1));
  assert.ok((await discoverAugmentEditors({ home: f.home, env: {} })).issues.includes('VSCODE_DISCOVERY_INCOMPLETE'));
});
test('JetBrains XML never resolves entities or inflates oversized entries', async (t) => {
  const f = await homeFixture(t);
  await write(jar(f.home), zip('<!DOCTYPE plugin [<!ENTITY remote SYSTEM "file:///not-read">]>' + xml()));
  assert.equal((await discoverAugmentEditors({ home: f.home, env: {} })).installations.length, 0);
  await write(jar(f.home), zip(xml() + ' '.repeat(65537)));
  assert.equal((await discoverAugmentEditors({ home: f.home, env: {} })).installations.length, 0);
});
test('Windows stays held rather than asserting Unix file modes are ACLs', async (t) => {
  const f = await homeFixture(t);
  assert.deepEqual((await discoverAugmentEditors({ home: f.home, platform: 'win32' })).issues, ['WINDOWS_ACL_NOT_QUALIFIED']);
});
test('user hook profile never uses CLI workspace settings; project privacy override remains available', async (t) => {
  const f = await fixture(t);
  assert.equal(ideRegistrationProfile(f.context).settings, path.join(f.home, '.augment/settings.json'));
  assert.throws(() => ideRegistrationProfile({ ...f.context, scope: 'project' }), /PROJECT_HOOKS_NOT_QUALIFIED/);
  await write(path.join(f.projectDir, '.augment/futureagi.json'), JSON.stringify({ schemaVersion: 1, captureContent: false }));
  assert.equal((await core.resolveConfig(f.options)).captureContent, false);
});
test('detected editors cannot bypass the independent GUI gates or write hooks', async (t) => {
  const f = await fixture(t); await write(vscode(f.home), vsManifest); await write(jar(f.home), zip(xml()));
  assert.deepEqual(await augmentIdeAdapter.detect(f.context), { supported: false, reason: IDE_HOLD });
  await assert.rejects(augmentIdeAdapter.install(f.context), new RegExp(IDE_HOLD));
  await assert.rejects(fs.stat(path.join(f.home, '.augment/settings.json')), /ENOENT/);
  const status = await core.inspectStatus({ ...f.options, adapter: augmentIdeAdapter });
  assert.equal(status.registration, 'NOT_REGISTERED'); assert.equal(status.activation, 'NOT_OBSERVED');
  assert.equal(status.adapterDetail, IDE_HOLD); assert.match(status.hostVersion, /vscode@0.904.1/); assert.match(status.hostVersion, /intellij@0.491.0-stable/);
  assert.doesNotMatch(JSON.stringify(status), /synthetic-api|synthetic-secret/);
});

test('shared runtime handles IDE identity with stripped GUI PATH, file changes, errors and opt-out', async (t) => {
  const f = await fixture(t);
  // Package primitive only: this is deliberately NOT a real editor run or a public qualification bypass.
  await augmentHookRegistration.install(f.context);
  const settings = JSON.parse(await fs.readFile(path.join(f.home, '.augment/settings.json'), 'utf8'));
  const command = settings.hooks.Stop[0].hooks[0].command;
  const env = { HOME: f.home, PATH: '', AUGMENT_PROJECT_DIR: f.projectDir };
  const dispatch = async (event, extra = {}, overrides = {}) => {
    const result = await run('/bin/sh', ['-c', command], { ...env, ...overrides }, JSON.stringify({ hook_event_name: event, conversation_id: 'editor-schema-session', ...extra }), f.projectDir);
    assert.deepEqual(result, { code: 0, stdout: '', stderr: '' });
  };
  await dispatch('PostToolUse', { tool_name: 'save-file', tool_output: 'written', file_changes: [{ changeType: 'create', path: 'sample.ts', content: 'FILE_CANARY' }] });
  await dispatch('PostToolUse', { tool_name: 'launch-process', tool_error: 'ERROR_CANARY' });
  await dispatch('Stop', { agent_stop_cause: 'end_turn', conversation: { userPrompt: 'PROMPT_CANARY', agentTextResponse: 'REPLY_CANARY' } });
  const spans = f.spans(); assert.equal(spans.length, 3);
  assert.equal(spans.filter((s) => s.status.code === 2).length, 1);
  assert.ok(spans.every((s) => attrs(s)['fi.span.kind'] !== 'LLM'));
  assert.match(JSON.stringify(spans), /FILE_CANARY/); assert.match(JSON.stringify(spans), /PROMPT_CANARY/);
  assert.doesNotMatch(JSON.stringify(spans), /token_count|usage.cost/);
  const status = await core.inspectStatus(f.options); assert.equal(status.activation, 'OBSERVED');
  // Operational status may observe a hook, but cannot tell which GUI emitted it.
  assert.equal((await augmentIdeAdapter.inspect(f.context)).activated, false);
  const cli = process.env.FI_PACKED_CLI || path.resolve(__dirname, '../../traceai_agents/dist/cli.js');
  const statusResult = await run(process.execPath, [cli, 'status', 'augment-ide', '--json'], {
    ...env, XDG_STATE_HOME: f.env.XDG_STATE_HOME, XDG_CONFIG_HOME: f.env.XDG_CONFIG_HOME,
  });
  assert.equal(statusResult.code, 2);
  assert.equal(JSON.parse(statusResult.stdout).activation, 'HOOK_OBSERVED_EDITOR_UNVERIFIED');
  assert.equal(JSON.parse(statusResult.stdout).qualification, IDE_HOLD);
  await core.disable(f.options);
  await dispatch('Stop', { agent_stop_cause: 'end_turn' }); assert.equal(f.spans().length, 3);
  await core.enable(f.options);
  await dispatch('Stop', { agent_stop_cause: 'end_turn', conversation: { userPrompt: 'PRIVATE_CANARY' } }, { FI_AGENT_CAPTURE_CONTENT: 'false' });
  assert.equal(f.spans().length, 4); assert.doesNotMatch(JSON.stringify(f.spans()), /PRIVATE_CANARY/);
  await dispatch('Stop', { agent_stop_cause: 'end_turn' }, { FI_AGENT_ENABLED: 'false' }); assert.equal(f.spans().length, 4);
});
test('shared registration conflicts are not silently overwritten across CLI and IDE', async (t) => {
  const f = await fixture(t); await augmentHookRegistration.install(f.context);
  const config = await core.resolveConfig({ ...f.options, agent: 'augment' });
  await assert.rejects(augmentHookRegistration.install({ ...f.context, config }), /AUGMENT_HOOK_CONFLICT/);
  await augmentHookRegistration.uninstall({ ...f.context, config }); // CLI owns nothing here.
  assert.ok((await augmentHookRegistration.inspect(f.context)).registered);
});

test('unified CLI holds setup/enable/update/run before credentials, registration, or validation requests', async (t) => {
  const f = await homeFixture(t);
  const cli = process.env.FI_PACKED_CLI || path.resolve(__dirname, '../../traceai_agents/dist/cli.js');
  const env = { HOME: f.home, PATH: '', FI_API_KEY: 'synthetic-api', FI_SECRET_KEY: 'synthetic-secret', FI_OTLP_ENDPOINT: 'http://127.0.0.1:1/tracer/v1/traces' };
  for (const cmd of ['setup', 'enable', 'update', 'run']) {
    const result = await run(process.execPath, [cli, cmd, 'augment-ide', '--json'], env);
    assert.equal(result.code, 2); assert.equal(result.stderr, '');
    assert.equal(JSON.parse(result.stdout).code, IDE_HOLD);
    assert.deepEqual(await fs.readdir(f.home), []);
  }
  const status = await run(process.execPath, [cli, 'status', 'augment-ide', '--json'], env);
  assert.equal(status.code, 2); assert.equal(JSON.parse(status.stdout).qualification, IDE_HOLD);
  assert.equal(JSON.parse(status.stdout).adapterDetail, IDE_HOLD);
  const disabled = await run(process.execPath, [cli, 'disable', 'augment-ide', '--json'], env);
  assert.equal(disabled.code, 0); assert.equal(JSON.parse(disabled.stdout).code, 'DISABLED');
});
