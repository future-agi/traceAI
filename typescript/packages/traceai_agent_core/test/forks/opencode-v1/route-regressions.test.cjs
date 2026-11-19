const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const c = require('../../../dist/src/index.js');

async function fixture(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'opencode-route-'));
  const projectDir = path.join(home, 'repo'); await fs.mkdir(projectDir);
  const requests = [];
  const server = http.createServer(async (req, res) => {
    let raw = ''; for await (const b of req) raw += b;
    requests.push({ headers: req.headers, body: JSON.parse(raw), url: req.url });
    res.setHeader('content-type', 'application/json'); res.end('{}');
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(async () => { server.closeAllConnections(); await new Promise(r => server.close(r)); await fs.rm(home, { recursive: true, force: true }); });
  const endpoint = `http://127.0.0.1:${server.address().port}/traces`;
  const options = { agent: 'opencode-v1', home, projectDir, env: {} };
  const keys = { FI_API_KEY: 'synthetic-api', FI_SECRET_KEY: 'synthetic-secret' };
  const setup = extra => c.setup({ ...options, credentials: { apiKey: keys.FI_API_KEY, secretKey: keys.FI_SECRET_KEY }, projectName: 'first-project', otlpEndpoint: endpoint, configureOnly: true, ...extra });
  await setup();
  const paths = await c.configPaths(options);
  return { home, options, keys, setup, paths, endpoint, requests };
}
async function exportOne(config) {
  const tracer = c.createTurnTracer({ config });
  tracer.startTurn({ sessionId: 's', turnId: 't', input: 'SYNTHETIC-PROMPT' }).end();
  await tracer.shutdown();
}

test('environment keys do not approve repository endpoint, project or alternate profile', async t => {
  const f = await fixture(t);
  const alternate = { endpoint: f.endpoint + '/unapproved', projectName: 'other-project' };
  await c.updateJson(f.paths.credentials, store => ({ ...store, profiles: { ...store.profiles, alternate: { ...Object.values(store.profiles)[0], ...alternate } } }));
  for (const layer of [
    { transport: { endpoint: alternate.endpoint } },
    { projectName: alternate.projectName },
    { profile: 'alternate' },
    { transport: { endpoint: alternate.endpoint }, projectName: alternate.projectName },
  ]) {
    await c.writeProtected(f.paths.project, JSON.stringify({ schemaVersion: 1, ...layer }));
    const options = { ...f.options, env: f.keys };
    const config = await c.resolveConfig(options);
    assert.equal(config.destinationApproved, false, JSON.stringify(layer));
    assert.equal(config.enabled, false);
    const before = f.requests.length;
    await c.inspectStatus({ ...options, check: true }); await exportOne(config);
    assert.equal(f.requests.length, before, 'neither validation nor spans send credentials to an unapproved route');
  }
  // Approving the endpoint alone cannot silently approve a repo-selected project.
  const partial = await c.resolveConfig({ ...f.options, env: { ...f.keys, FI_OTLP_ENDPOINT: alternate.endpoint } });
  assert.equal(partial.destinationApproved, false);
  const explicit = await c.resolveConfig({ ...f.options, env: { ...f.keys, FI_OTLP_ENDPOINT: alternate.endpoint, FI_PROJECT_NAME: alternate.projectName } });
  assert.equal(explicit.destinationApproved, true);
  await exportOne(explicit); assert.equal(f.requests.at(-1).url, '/traces/unapproved');
  await c.writeProtected(f.paths.project, JSON.stringify({ schemaVersion: 1, transport: { endpoint: f.endpoint }, projectName: 'first-project' }));
  assert.equal((await c.resolveConfig({ ...f.options, env: f.keys })).destinationApproved, true, 'saved exact binding remains valid');
});

test('status evidence is scoped to destination, project and installation generation', async t => {
  const f = await fixture(t);
  await exportOne(await c.resolveConfig(f.options));
  const first = await c.inspectStatus(f.options);
  assert.equal(first.activation, 'OBSERVED'); assert.equal(first.delivery.exported, 1);
  for (const env of [
    { ...f.keys, FI_PROJECT_NAME: 'second-project' },
    { ...f.keys, FI_OTLP_ENDPOINT: f.endpoint + '/second' },
  ]) {
    const status = await c.inspectStatus({ ...f.options, env, check: true });
    assert.equal(status.activation, 'NOT_OBSERVED'); assert.equal(status.delivery.exported, 0);
    assert.equal(status.delivery.lastAcceptedAt, undefined); assert.ok(status.issues.includes('ACTIVATION_NOT_OBSERVED'));
  }
  await f.setup();
  assert.equal((await c.inspectStatus(f.options)).activation, 'NOT_OBSERVED', 'reinstall invalidates activation');
  await exportOne(await c.resolveConfig(f.options));
  await c.disable(f.options); await c.enable(f.options);
  assert.equal((await c.inspectStatus(f.options)).delivery.exported, 0, 'new enable generation has no previous exports');
});

test('project setup updates safe 0644 settings but never loosens credential permissions', async t => {
  const f = await fixture(t);
  await f.setup({ scope: 'project' });
  await fs.chmod(f.paths.project, 0o644);
  assert.equal((await f.setup({ scope: 'project' })).configured, true);
  assert.equal((await fs.stat(f.paths.project)).mode & 0o777, 0o600);
  await fs.chmod(f.paths.project, 0o666);
  await assert.rejects(f.setup({ scope: 'project' }), /UNSAFE_FILE/);
  await fs.chmod(f.paths.project, 0o600);
  await fs.chmod(f.paths.credentials, 0o644);
  await assert.rejects(f.setup({ scope: 'project' }), /UNSAFE_FILE/);
});

test('unsafe external-volume project layer falls back to protected user route with a diagnostic', async t => {
  const f = await fixture(t);
  await c.writeProtected(f.paths.project, JSON.stringify({ schemaVersion: 1, enabled: false, transport: { endpoint: f.endpoint + '/unsafe' } }));
  await fs.chmod(f.options.projectDir, 0o775);
  const config = await c.resolveConfig(f.options);
  assert.equal(config.enabled, true); assert.equal(config.endpoint, f.endpoint);
  assert.ok(config.issues.includes('PROJECT_CONFIG_IGNORED'));
  await exportOne(config);
  const status = await c.inspectStatus(f.options);
  assert.equal(status.delivery.exported, 1); assert.ok(status.issues.includes('PROJECT_CONFIG_IGNORED'));
});
