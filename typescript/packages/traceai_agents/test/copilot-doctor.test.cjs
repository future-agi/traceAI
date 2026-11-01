const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { Runtime, runtimeRequest } = require('../dist/src/runtime/daemon.js');
const { AdapterRegistry } = require('../dist/src/adapters.js');
const { copilotCliAdapter } = require('../dist/src/profiles/copilot.js');
const { paths, saveConfig } = require('../dist/src/config.js');
const { protectedDirectory } = require('../dist/src/security/files.js');
const { FileSecretStore, newCredentials } = require('../dist/src/security/credentials.js');
const { main } = require('../dist/src/cli/main.js');
const fixture = require('../../../../integrations/coding-agents/fixtures/source/copilot-cli-1.0.98.json');

test('preview doctor reports owned installation and durable local receipt, not remote persistence', async (t) => {
  const root = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'fi-copilot-doctor-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const prior = [process.env.FI_AGENT_CONFIG_HOME, process.env.FI_AGENT_STATE_HOME];
  process.env.FI_AGENT_CONFIG_HOME = path.join(root, 'config');
  process.env.FI_AGENT_STATE_HOME = path.join(root, 'state');
  const p = paths();
  const binding = {
    id: '00000000-0000-4000-8000-000000000001', project: 'synthetic-test',
    endpoint: 'http://127.0.0.1:1/v1/traces', approvedOrigin: 'http://127.0.0.1:1', loopbackTest: true,
  };
  const registry = new AdapterRegistry([copilotCliAdapter]);
  const settings = { version: '0.1.0', entry: path.join(root, 'unused'), storageEntry: path.resolve(__dirname, '../dist/src/runtime/storage-child.js'), registry };
  let runtime;
  const outputs = [];
  const write = process.stdout.write;
  process.stdout.write = function (chunk, ...args) { outputs.push(String(chunk)); return true; };
  try {
    protectedDirectory(p.config, true);
    protectedDirectory(p.state, true);
    saveConfig(p, { schema_version: 1, profiles: { fixture: { binding, storage: 'file', credentialRef: 'fixture' } }, routes: {} });
    await new FileSecretStore(p.credentials).set('fixture', newCredentials({ api_key: 'synthetic-only', secret_key: 'synthetic-only' }));
    runtime = new Runtime(p, settings.storageEntry, settings.version, registry);
    await runtime.start();
    assert.equal(await main(['enable', 'copilot', '--surface', 'cli', '--profile', 'fixture', '--preview'], settings), 0);
    let status = await runtimeRequest(p.socket, { operation: 'status' });
    assert.equal(status.agents[0].activation.installed, true);
    const lease = await runtimeRequest(p.socket, { operation: 'native-open', route: 'copilot.cli' });
    const response = await fetch(lease.endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + lease.token }, body: JSON.stringify(fixture) });
    await response.arrayBuffer();
    assert.equal(response.status, 200);
    status = await runtimeRequest(p.socket, { operation: 'status' });
    assert.equal(status.agents[0].activation.activated, true);
    assert.equal(await main(['doctor', 'copilot', '--surface', 'cli', '--profile', 'fixture', '--preview'], settings), 0);
    const doctor = JSON.parse(outputs.at(-1));
    assert.equal(doctor.state, 'activated');
    assert.equal(doctor.activation.installed, true);
    assert.equal(doctor.activation.activated, true);
    assert.equal(doctor.persistence, 'unknown');
    await runtimeRequest(p.socket, { operation: 'native-close', id: lease.id });
  } finally {
    process.stdout.write = write;
    await runtime?.stop();
    if (prior[0] === undefined) delete process.env.FI_AGENT_CONFIG_HOME;
    else process.env.FI_AGENT_CONFIG_HOME = prior[0];
    if (prior[1] === undefined) delete process.env.FI_AGENT_STATE_HOME;
    else process.env.FI_AGENT_STATE_HOME = prior[1];
  }
});
