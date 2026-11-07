const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { createVscodeAdapter } = require('../dist/vscode');
const { setup, resolveConfig, inspectStatus, disable, enable, uninstall } = require('@traceai/agent-core');
const { installationPath, heartbeatPath, readInstallation } = require('@traceai/vscode-extension');
async function fixture(t, preinstalled = false) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'vscode-life-'));
  t.after(() => fs.rm(home, {recursive:true, force:true}));
  const env = { PATH: home, FI_AGENT_CONFIG_DIR: path.join(home, 'config'), XDG_STATE_HOME: path.join(home, 'state'), FI_COPILOT_VSCODE_USER_DATA_DIR: path.join(home, 'editor') };
  await fs.writeFile(path.join(home, 'code'), '#!/bin/sh\nexit 0\n', {mode:0o700});
  const calls = []; let installed = preinstalled;
  const adapter = createVscodeAdapter({ home, env, platform: 'linux', runCommand: async (_cli, args) => {
    calls.push(args);
    if (args.includes('--install-extension')) { assert.ok(args.at(-1).endsWith('extension.vsix')); await fs.access(args.at(-1)); installed = true; }
    if (args.includes('--uninstall-extension')) installed = false;
    return args.includes('--list-extensions') && installed ? `future-agi.vscode-extension@${require('@traceai/vscode-extension/package.json').version}\n` : '';
  } });
  const settings = path.join(home, 'editor/User/settings.json');
  await fs.mkdir(path.dirname(settings), {recursive:true});
  const original = '// original\n{"editor.fontSize": 14}\n'; await fs.writeFile(settings, original);
  let validations = 0;
  const server = http.createServer((req, res) => { validations++; req.resume(); res.setHeader('content-type', 'application/json'); res.end('{}'); });
  await new Promise(r => server.listen(0, '127.0.0.1', r)); t.after(() => new Promise(r => server.close(r)));
  const options = { home, env, agent:'copilot-vscode', adapter, scope:'user', projectName:'synthetic-project', approveDestination: true,
    otlpEndpoint:`http://127.0.0.1:${server.address().port}/v1/traces`, credentials: {apiKey:'synthetic-api', secretKey:'synthetic-secret'} };
  return { home, env, calls, adapter, options, settings, original, validations: () => validations };
}
test('validated core setup installs VSIX, journals settings and reports live heartbeat separately', async t => {
  const f = await fixture(t);
  const result = await setup(f.options);
  assert.equal(result.configured, true); assert.equal(result.registered, true); assert.ok(f.validations() > 0);
  assert.equal(f.calls.filter(a => a.includes('--install-extension')).length, 1);
  const r = await readInstallation(installationPath(f)); assert.equal(r.scope, 'user'); assert.equal(r.extensionOwned, true);
  assert.doesNotMatch(await fs.readFile(f.settings, 'utf8'), /synthetic-api|synthetic-secret/);
  let status = await inspectStatus(f.options); assert.equal(status.activation, 'NOT_OBSERVED');
  const heartbeat = heartbeatPath(f); await fs.mkdir(path.dirname(heartbeat), {recursive:true, mode:0o700});
  await fs.writeFile(heartbeat, JSON.stringify({id:r.id, at:new Date().toISOString(), active:true, exported:1, dropped:0}), {mode:0o600});
  status = await inspectStatus(f.options); assert.equal(status.activation, 'OBSERVED');
  await setup(f.options); assert.equal(f.calls.filter(a => a.includes('--install-extension')).length, 1);
});
test('disable, enable and uninstall restore settings without deleting unrelated keys', async t => {
  const f = await fixture(t); await setup(f.options);
  await disable(f.options); await f.adapter.onEnabledChange(f.options, false);
  assert.deepEqual(JSON.parse((await fs.readFile(f.settings, 'utf8')).replace('// original', '')), { 'editor.fontSize':14 });
  await enable(f.options); await f.adapter.onEnabledChange(f.options, true);
  assert.equal(JSON.parse((await fs.readFile(f.settings, 'utf8')).replace('// original', ''))['chat.agentHost.otel.enabled'], true);
  const removed = await uninstall(f.options); assert.equal(removed.registered, false);
  assert.equal(f.calls.filter(a => a.includes('--uninstall-extension')).length, 1);
  assert.equal((await resolveConfig(f.options)).enabled, false);
  assert.equal(await readInstallation(installationPath(f)), undefined);
});
test('pre-existing extension remains installed on uninstall', async t => {
  const f = await fixture(t, true); await setup(f.options); await uninstall(f.options);
  assert.equal(f.calls.filter(a => a.includes('--uninstall-extension')).length, 0);
});
test('explicit VS Code profiles are rejected before installing anything', async t => {
  const f = await fixture(t); f.env.FI_COPILOT_VSCODE_PROFILE_ID = 'named-profile';
  const config = await resolveConfig(f.options);
  assert.equal((await f.adapter.detect({config, scope:'user'})).supported, false);
  await assert.rejects(f.adapter.install({config,scope:'user'}), /DEFAULT_PROFILE_REQUIRED/);
  assert.equal(f.calls.length, 0);
});
test('privacy switches turn native capture off and settings conflicts keep ownership recoverable', async t => {
  const f = await fixture(t); f.env.FI_HIDE_INPUTS = 'true'; await setup(f.options);
  const text = await fs.readFile(f.settings, 'utf8'); assert.match(text, /"chat.agentHost.otel.captureContent": false/);
  await fs.writeFile(f.settings, text.replace('"chat.agentHost.otel.enabled": true', '"chat.agentHost.otel.enabled": false'));
  assert.equal((await uninstall(f.options)).registered, true);
  assert.ok(await readInstallation(installationPath(f)));
});
