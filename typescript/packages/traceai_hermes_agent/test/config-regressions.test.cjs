const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { spawnSync } = require('node:child_process');
const core = require('@traceai/agent-core');
// e2b_native.sh supplies an isolated interpreter with the Python runtime installed.
const crossTest = process.env.HERMES_RESOLVER_PYTHON ? test : test.skip;

async function fixture(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'hermes-isolation-'));
  const server = http.createServer((q, r) => {
    q.resume();
    q.on('end', () => { r.setHeader('Content-Type', 'application/json'); r.end('{}'); });
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise(r => server.close(r));
    await fs.rm(home, { recursive: true, force: true });
  });
  const env = { HOME: home, FI_AGENT_CONFIG_DIR: path.join(home, 'config'), XDG_STATE_HOME: path.join(home, 'state') };
  const options = name => ({ agent: 'hermes', home, env, agentDir: path.join(home, name) });
  const endpoint = `http://127.0.0.1:${server.address().port}/tracer/v1/traces`;
  const setup = (name, key, extra = {}) => core.setup({ ...options(name), configureOnly: true,
    otlpEndpoint: endpoint, projectName: 'coding-agents',
    credentials: { apiKey: key, secretKey: `secret-${key}` }, ...extra });
  return { home, env, options, endpoint, setup };
}

function pythonResolution(f, name, project, env = {}) {
  assert.ok(process.env.HERMES_RESOLVER_PYTHON, 'Run this cross-language suite with HERMES_RESOLVER_PYTHON');
  const result = spawnSync(process.env.HERMES_RESOLVER_PYTHON, ['-I', '-c',
    "import json,os,sys; from pathlib import Path; from traceai_hermes_agent._config import resolve; c=resolve(os.environ.get,host=Path(sys.argv[1]),project=Path(sys.argv[2]) if len(sys.argv)>2 else None); print(json.dumps({k:c[k] for k in ('enabled','api_key','secret_key','issues')}))",
    f.options(name).agentDir, ...(project ? [project] : [])], { env: { ...f.env, ...env }, encoding: 'utf8', timeout: 5000 });
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

crossTest('independent Hermes homes retain their accounts across repeat setup and rotation in TS and Python', async t => {
  const f = await fixture(t);
  assert.equal((await f.setup('a', 'synthetic-a')).configured, true);
  const before = await core.resolveConfig(f.options('a'));
  assert.equal((await f.setup('b', 'synthetic-b')).configured, true);
  const after = await core.resolveConfig(f.options('a'));
  const b = await core.resolveConfig(f.options('b'));
  assert.equal(after.profile, before.profile);
  assert.notEqual(after.profile, b.profile);
  for (const [name, key] of [['a', 'synthetic-a'], ['b', 'synthetic-b']]) {
    const c = await core.resolveConfig(f.options(name));
    assert.equal(c.enabled, true); assert.equal(c.credentials.apiKey, key);
    assert.deepEqual(pythonResolution(f, name), { enabled: true, api_key: key, secret_key: `secret-${key}`, issues: [] });
  }
  await f.setup('b', 'rotated-b');
  assert.equal((await core.resolveConfig(f.options('a'))).credentials.apiKey, 'synthetic-a');
  assert.equal(pythonResolution(f, 'a').api_key, 'synthetic-a');
  assert.equal(pythonResolution(f, 'b').api_key, 'rotated-b');
  // No implicit cross-home reuse of the shared store's first/default profile.
  const fresh = await core.setup({ ...f.options('unconfigured'), configureOnly: true });
  assert.equal(fresh.code, 'CREDENTIALS_REQUIRED');
  // A copied host reference is not approval for the other home's account.
  const paths = await core.configPaths(f.options('b'));
  await core.writeProtected(paths.user, JSON.stringify(await core.readSettings(after.paths.user)));
  assert.equal((await core.resolveConfig(f.options('b'))).enabled, false);
  assert.equal(pythonResolution(f, 'b').enabled, false);
});

crossTest('migrating one legacy shared profile never overwrites another home reference', async t => {
  const f = await fixture(t);
  const a = await core.configPaths(f.options('a'));
  const b = await core.configPaths(f.options('b'));
  const legacy = core.destinationId(f.endpoint, 'coding-agents');
  await core.writeProtected(a.credentials, JSON.stringify({ schemaVersion: 1, defaultProfile: legacy, profiles: {
    [legacy]: { apiKey: 'legacy-a', secretKey: 'legacy-secret', endpoint: f.endpoint, projectName: 'coding-agents', captureContent: true },
  } }));
  const settings = JSON.stringify({ schemaVersion: 1, enabled: true, profile: legacy, projectName: 'coding-agents' });
  await core.writeProtected(a.user, settings); await core.writeProtected(b.user, settings);
  await f.setup('b', 'new-b');
  assert.equal((await core.resolveConfig(f.options('a'))).credentials.apiKey, 'legacy-a');
  assert.equal(pythonResolution(f, 'a').api_key, 'legacy-a');
  assert.equal(pythonResolution(f, 'b').api_key, 'new-b');
});

crossTest('a shared workspace cannot authorize another Hermes home after scoped setup', async t => {
  const f = await fixture(t);
  const projectDir = path.join(f.home, 'workspace'); await fs.mkdir(projectDir);
  await f.setup('a', 'project-a', { scope: 'project', projectDir });
  const a = await core.resolveConfig({ ...f.options('a'), projectDir });
  assert.equal(a.enabled, true);
  assert.equal(pythonResolution(f, 'a', projectDir).enabled, true);
  assert.equal((await core.resolveConfig({ ...f.options('b'), projectDir })).enabled, false);
  assert.equal(pythonResolution(f, 'b', projectDir).enabled, false);
  await assert.rejects(core.setup({ ...f.options('b'), projectDir, scope: 'project', configureOnly: true }),
    /DESTINATION_APPROVAL_REQUIRED/); // unified core rejects transplanted routing before validation
  // Repeating an approved project setup may reuse only that home's bound pair.
  const repeat = await core.setup({ ...f.options('a'), projectDir, scope: 'project', configureOnly: true });
  assert.equal(repeat.configured, true);
});

crossTest('a repository cannot adopt an unbound legacy account through repeat setup', async t => {
  const f = await fixture(t);
  const projectDir = path.join(f.home, 'workspace'); await fs.mkdir(projectDir);
  const paths = await core.configPaths({ ...f.options('a'), projectDir });
  await core.writeProtected(paths.credentials, JSON.stringify({ schemaVersion: 1, defaultProfile: 'legacy', profiles: {
    legacy: { apiKey: 'legacy-key', secretKey: 'legacy-secret', endpoint: f.endpoint, projectName: 'coding-agents' },
  } }));
  await core.writeProtected(paths.project, JSON.stringify({ hermes: { schemaVersion: 1, profile: 'legacy' } }));
  await assert.rejects(core.setup({ ...f.options('a'), scope: 'project', projectDir, configureOnly: true }),
    /DESTINATION_APPROVAL_REQUIRED/);
  assert.equal(pythonResolution(f, 'a', projectDir).enabled, false);
});

crossTest('special-file reads are bounded and unsafe workspace config falls back with diagnostics', async t => {
  const f = await fixture(t);
  await f.setup('a', 'synthetic-a');
  const projectDir = path.join(f.home, 'workspace'); await fs.mkdir(projectDir);
  const p = await core.configPaths({ ...f.options('a'), projectDir });
  await fs.mkdir(path.dirname(p.project), { mode: 0o700 });
  const fifo = spawnSync('mkfifo', ['-m', '600', p.project]);
  assert.equal(fifo.status, 0);
  // A subprocess deadline also bounds this test if open-before-fstat regresses.
  const probe = spawnSync(process.execPath, ['-e',
    `require(${JSON.stringify(require.resolve('@traceai/agent-core'))}).readProtected(process.argv[1]).then(()=>process.exit(1),e=>{if(e.message!=='UNSAFE_FILE')process.exit(2)})`, p.project],
    { timeout: 3000, encoding: 'utf8' });
  assert.equal(probe.status, 0, String(probe.error || probe.stderr));
  const config = await core.resolveConfig({ ...f.options('a'), projectDir });
  assert.equal(config.enabled, true);
  assert.deepEqual(config.issues, ['PROJECT_CONFIG_IGNORED']);
  assert.equal(pythonResolution(f, 'a', projectDir).enabled, true);
  await fs.unlink(p.project);
  await fs.writeFile(p.project, '{"hermes":{"schemaVersion":1,"enabled":false}}');
  await fs.chmod(projectDir, 0o775); // /Volumes/<disk> and shared mount ancestry
  const status = await core.inspectStatus({ ...f.options('a'), projectDir });
  assert.equal(status.enabled, true);
  assert.ok(status.issues.includes('PROJECT_CONFIG_IGNORED'));
  assert.deepEqual(pythonResolution(f, 'a', projectDir).issues, ['PROJECT_CONFIG_IGNORED']);
});

for (const layer of [{ projectName: 'unapproved' }, { profile: 'unapproved' },
  { transport: { endpoint: 'https://unapproved.invalid/traces' } }]) {
  crossTest(`inherited keys cannot approve repository routing ${JSON.stringify(layer)}`, async t => {
    const f = await fixture(t); await f.setup('a', 'synthetic-a');
    const projectDir = path.join(f.home, 'workspace'); await fs.mkdir(projectDir);
    const p = await core.configPaths({ ...f.options('a'), projectDir });
    await core.writeProtected(p.project, JSON.stringify({ hermes: { schemaVersion: 1, ...layer } }));
    const env = { FI_API_KEY: 'override-api', FI_SECRET_KEY: 'override-secret' };
    const ts = await core.resolveConfig({ ...f.options('a'), projectDir, env: { ...f.env, ...env } });
    assert.equal(ts.enabled, false);
    assert.equal(pythonResolution(f, 'a', projectDir, env).enabled, false);
  });
}

for (const transport of [null, [], 'bad', { endpoint: 7 }, { baseUrl: false },
  { endpoint: '' }, { baseUrl: 'https://example.invalid/path' }, { unknown: 'x' }]) {
  crossTest(`malformed transport ${JSON.stringify(transport)} falls back in both runtimes`, async t => {
    const f = await fixture(t); await f.setup('a', 'synthetic-a');
    const projectDir = path.join(f.home, 'workspace'); await fs.mkdir(projectDir);
    const p = await core.configPaths({ ...f.options('a'), projectDir });
    await core.writeProtected(p.project, JSON.stringify({ hermes: { schemaVersion: 1, transport } }));
    const ts = await core.resolveConfig({ ...f.options('a'), projectDir });
    const py = pythonResolution(f, 'a', projectDir);
    assert.equal(ts.enabled, true); assert.equal(py.enabled, true);
    assert.deepEqual(ts.issues, ['PROJECT_CONFIG_IGNORED']);
    assert.deepEqual(py.issues, ts.issues);
    assert.equal(ts.credentials.apiKey, py.api_key);
    assert.equal(ts.endpoint, f.endpoint);
  });
}
