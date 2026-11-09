const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { spawn } = require('node:child_process');
const cli = path.resolve(__dirname, '../dist/cli.js');
const hold = 'VSCODE_GUI_AND_TRANSPORT_QUALIFICATION_REQUIRED';
async function fixture(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'fi-vscode-cli-'));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const config = path.join(home, 'config');
  const env = { HOME: home, FI_AGENT_CONFIG_DIR: config, XDG_STATE_HOME: path.join(home, 'state'), FI_COPILOT_VSCODE_USER_DATA_DIR: path.join(home, 'editor'), PATH: '' };
  const run = (args, overrides = {}) => new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [cli, ...args], { env: { ...env, ...overrides } });
    let stdout = '', stderr = '';
    child.stdout.on('data', d => stdout += d);
    child.stderr.on('data', d => stderr += d);
    child.on('error', reject);
    child.on('close', code => resolve({ code, stdout, stderr }));
    child.stdin.end();
  });
  return { home, config, run };
}
test('editor setup reaches shared key validation without a qualification blocker', async (t) => {
  const f = await fixture(t);
  let calls = 0;
  const server = http.createServer((q, r) => { calls++; q.resume(); r.setHeader('content-type', 'application/json'); r.end('{}'); });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(() => new Promise(r => server.close(r)));
  const env = { FI_API_KEY: 'synthetic-private-api', FI_SECRET_KEY: 'synthetic-private-secret', FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/v1/traces` };
  const result = await f.run(['setup', 'copilot-vscode', '--json'], env);
  assert.equal(result.code, 2); // No code host in this stripped fixture.
  assert.notEqual(JSON.parse(result.stdout).code, hold);
  assert.equal(JSON.parse(result.stdout).registered, false);
  assert.ok(!/synthetic-private/.test(result.stdout + result.stderr));
  assert.ok(calls > 0);
});
test('status diagnoses a missing editor extension in stripped GUI-like environment', async (t) => {
  const f = await fixture(t);
  const list = await f.run(['status', '--json']);
  assert.equal(JSON.parse(list.stdout).agents.find(a => a.agent === 'copilot-vscode').qualification, 'QUALIFIED');
  const result = await f.run(['status', 'copilot-vscode', '--json']);
  assert.equal(result.code, 2);
  const status = JSON.parse(result.stdout);
  assert.equal(status.registration, 'NOT_REGISTERED');
  assert.equal(status.activation, 'NOT_OBSERVED');
  assert.equal(status.productReadback, 'unverified');
  assert.ok(status.issues.includes("VSCODE_EXTENSION_NOT_INSTALLED"));
});
test('disable and uninstall are offline and preserve user-owned telemetry settings', async (t) => {
  const f = await fixture(t);
  const settings = path.join(f.home, 'editor/User/settings.json');
  await fs.mkdir(path.dirname(settings), { recursive: true, mode: 0o700 });
  const original = '// do not own this\n{"github.copilot.chat.otel.enabled":true}\n';
  await fs.writeFile(settings, original, { mode: 0o644 });
  for (const command of ['disable', 'uninstall']) {
    const result = await f.run([command, 'copilot-vscode', '--json']);
    assert.equal(result.code, 0);
    assert.equal(await fs.readFile(settings, 'utf8'), original);
  }
  const state = JSON.parse(await fs.readFile(path.join(f.config, 'installation.json')));
  assert.equal(state.disabled['copilot-vscode'], true);
  const status = JSON.parse((await f.run(['status', 'copilot-vscode', '--json'])).stdout);
  assert.equal(status.enabled, false);
});
