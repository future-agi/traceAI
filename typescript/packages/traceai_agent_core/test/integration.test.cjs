const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { execFileSync } = require('node:child_process');
const c = require('../dist/src/index.js');
const attributes = s => Object.fromEntries(s.attributes.map(a => [a.key, Object.values(a.value)[0]]));
const allSpans = f => f.requests.flatMap(r => r.resourceSpans ?? []).flatMap(r => r.scopeSpans).flatMap(s => s.spans);
async function fixture(t, agent = 'codex', env = {}) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'unified-core-'));
  const requests = [];
  const server = http.createServer(async (req, res) => {
    let body = ''; for await (const chunk of req) body += chunk;
    requests.push(JSON.parse(body)); res.setHeader('content-type', 'application/json'); res.end('{}');
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(async () => { server.closeAllConnections(); await new Promise(r => server.close(r)); await fs.rm(home, { recursive: true, force: true }); });
  const endpoint = `http://127.0.0.1:${server.address().port}/traces`;
  const options = { home, agent, env };
  const setup = extra => c.setup({ ...options, credentials: { apiKey: 'synthetic-api', secretKey: 'synthetic-secret' }, otlpEndpoint: endpoint, configureOnly: true, ...extra });
  await setup();
  return { home, requests, options, endpoint, setup, config: await c.resolveConfig(options) };
}

test('generic terminal stops preserve fatal provider errors but recovered retries succeed', async t => {
  const f = await fixture(t), tracer = c.createTurnTracer({ config: f.config });
  for (const [id, terminal, recover] of [['fatal', 'success', false], ['missing-stop', 'incomplete', false], ['recovered', 'success', true]]) {
    const turn = tracer.startTurn({ sessionId: 's', turnId: id });
    turn.child({ id: 'request-failed', kind: 'llm', fidelity: 'native-request' }).end({ outcome: 'error' });
    if (recover) turn.child({ id: 'request-retry', kind: 'llm', fidelity: 'native-request' }).end({ outcome: 'success' });
    turn.end({ outcome: terminal });
  }
  const launch = tracer.startTurn({ sessionId: 's', turnId: 'no-native-spans' });
  launch.end({ outcome: 'error', gap: 'MISSING_NATIVE_SPANS' });
  await tracer.shutdown();
  const roots = allSpans(f).filter(s => !s.parentSpanId);
  assert.equal(roots.length, 4);
  assert.equal(roots.filter(s => s.status.code === 2).length, 3);
  assert.equal(roots.filter(s => attributes(s)['fi.agent.outcome'] === 'success').length, 1);
});

test('failure envelopes hide result content even without a thrown exception', async t => {
  const f = await fixture(t, 'codex', { FI_HIDE_ERROR_DETAILS: 'true' });
  const tracer = c.createTurnTracer({ config: f.config });
  const turn = tracer.startTurn({ sessionId: 's', turnId: 'failure-payload' });
  // The adapter, not the content serializer, interprets the native failure envelope.
  turn.child({ id: 'tool', kind: 'tool', name: 'shell' }).end({ outcome: 'error', output: JSON.stringify({ isError: true, body: 'PRIVATE_FAILURE_PAYLOAD' }) });
  turn.end({ outcome: 'error', output: JSON.stringify({ success: false, error: 'PRIVATE_FAILURE_PAYLOAD' }) });
  await tracer.shutdown();
  assert(!JSON.stringify(f.requests).includes('PRIVATE_FAILURE_PAYLOAD'));
  assert(allSpans(f).every(s => s.status.code === 2));
});

test('on-by-default status is explicit and privacy reconfiguration fences queued content', async t => {
  const f = await fixture(t);
  assert.equal((await c.inspectStatus(f.options)).privacy, 'content-on-by-default');
  const tracer = c.createTurnTracer({ config: f.config });
  tracer.startTurn({ sessionId: 's', turnId: 'before-opt-out', input: 'PRIVATE_QUEUED_CONTENT' }).end();
  await f.setup({ captureContent: false });
  const before = f.requests.length;
  await tracer.shutdown();
  assert.equal(f.requests.length, before);
  assert.equal((await c.inspectStatus(f.options)).privacy, 'structure-only');
  assert(!JSON.stringify(f.requests).includes('PRIVATE_QUEUED_CONTENT'));
});

test('direct trusted privacy edits are rechecked immediately before every export', async t => {
  const f = await fixture(t), tracer = c.createTurnTracer({ config: f.config });
  tracer.startTurn({ sessionId: 's', turnId: 'queued', input: 'PRIVATE_QUEUED_CONTENT' }).end();
  await c.updateJson(f.config.paths.user, old => ({ ...old, captureContent: false }));
  const before = f.requests.length;
  await tracer.shutdown();
  assert.equal(f.requests.length, before);
  assert.equal(tracer.state.lastError, 'EXPORT_POLICY_REVOKED');
});

test('unreadable or malformed project layers fall back on every adapter, not only Cursor', async t => {
  for (const agent of ['cursor', 'codex', 'claude', 'goose', 'hermes', 'openclaw', 'pi', 'copilot-vscode']) {
    const f = await fixture(t, agent);
    const projectDir = path.join(f.home, 'repository'); await fs.mkdir(projectDir);
    const options = { ...f.options, projectDir }, paths = await c.configPaths(options);
    const value = { schemaVersion: 1, transport: null };
    await c.writeProtected(paths.project, JSON.stringify(paths.project.endsWith('/.futureagi/agents.json') ? { [agent]: value } : value));
    let resolved = await c.resolveConfig(options);
    assert.equal(resolved.enabled, true, agent);
    assert(resolved.issues.includes('PROJECT_CONFIG_IGNORED'), agent);
    await fs.chmod(projectDir, 0o775);
    resolved = await c.resolveConfig(options);
    assert.equal(resolved.enabled, true, agent);
    assert(resolved.issues.includes('PROJECT_CONFIG_IGNORED'), agent);
    await fs.chmod(projectDir, 0o700);
  }
});

test('FIFO project configuration cannot block hook progress or disable the approved user route', async t => {
  const f = await fixture(t, 'oh-my-pi');
  const projectDir = path.join(f.home, 'repository'); await fs.mkdir(projectDir);
  const options = { ...f.options, projectDir }, paths = await c.configPaths(options);
  await fs.mkdir(path.dirname(paths.project), { recursive: true, mode: 0o700 });
  execFileSync('mkfifo', [paths.project]);
  const config = await c.resolveConfig(options);
  assert(config.enabled); assert(config.issues.includes('PROJECT_CONFIG_IGNORED'));
  const tracer = c.createTurnTracer({ config });
  tracer.startTurn({ sessionId: 's', turnId: 'not-wedged' }).end();
  await tracer.shutdown();
  assert.equal(allSpans(f).length, 1);
});

test('nested JSON and shell Basic-auth headers are scrubbed before display truncation', () => {
  for (const key of ['secret_key', 'secret-key', 'secretKey', 'AWS_SECRET_ACCESS_KEY', 'clientSecretKey']) {
    for (const value of [
      JSON.stringify({ nested: { [key]: 'SYNTHETIC_PRIVATE_VALUE' } }),
      'shell output ' + JSON.stringify(JSON.stringify({ [key]: 'SYNTHETIC_PRIVATE_VALUE', safe: 'visible' })),
      `${key}="SYNTHETIC_PRIVATE_VALUE"`,
    ]) assert(!c.redact(value).includes('SYNTHETIC_PRIVATE_VALUE'), value);
  }
  for (const header of ['Authorization', 'Proxy-Authorization']) {
    const encoded = Buffer.from('synthetic:private-basic-value').toString('base64');
    const text = `curl -H '${header}: Basic ${encoded}' --data 'safe'`;
    assert(!c.redact(text).includes(encoded));
  }
});

test('text redaction handles Unicode credential keys without backtracking over slash runs', () => {
  const opaque = '\\'.repeat(100000) + ' public-tail';
  const started = Date.now();
  assert.equal(c.redact(opaque), opaque);
  assert(Date.now() - started < 1000);
  assert(!c.redact('shell \\u0073ecret_key="SYNTHETIC_PRIVATE_VALUE" visible').includes('SYNTHETIC_PRIVATE_VALUE'));
});

test('an adapter-owned outer lease writes atomically without a second nested lock', async t => {
  const f = await fixture(t), file = path.join(f.home, 'session.json');
  await c.withFileLock(file, async check => {
    await c.writeProtectedUnderLock(file, '{"ready":true}', null);
    await check();
    assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
  });
  assert.deepEqual(await c.readJson(file), { ready: true });
});

test('uninstall removes newly owned settings and empty host directories but preserves preexisting files', async t => {
  const f = await fixture(t);
  const own = f.config.paths.user;
  await c.uninstall(f.options);
  await assert.rejects(fs.stat(own), { code: 'ENOENT' });
  await assert.rejects(fs.stat(path.dirname(own)), { code: 'ENOENT' });
  await c.writeProtected(own, JSON.stringify({ schemaVersion: 1, captureContent: false }));
  await f.setup();
  await c.uninstall(f.options);
  assert.equal((await c.readSettings(own)).captureContent, false);
  assert.equal((await c.readJson(f.config.paths.manifest)).disabled.codex, true);
});

test('uninstall refuses to remove changed owned configuration', async t => {
  const f = await fixture(t);
  await c.updateJson(f.config.paths.user, old => ({ ...old, tags: ['keep-me'] }));
  await assert.rejects(c.uninstall(f.options), /CONCURRENT_SAVE/);
  assert.deepEqual((await c.readSettings(f.config.paths.user)).tags, ['keep-me']);
  assert.equal(c.collectionEnabled(f.config), false);
});

test('cloud setup never reads or writes saved credentials and removes its owned policy on uninstall', async t => {
  const f = await fixture(t);
  await fs.chmod(f.config.paths.credentials, 0o644); // Local stores are forbidden in the hosted path.
  const projectDir = path.join(f.home, 'cloud-repo'); await fs.mkdir(projectDir);
  const options = { home: f.home, agent: 'cursor-cloud', projectDir, scope: 'project',
    env: { FI_API_KEY: 'cloud-synthetic-api', FI_SECRET_KEY: 'cloud-synthetic-secret', FI_OTLP_ENDPOINT: f.endpoint, FI_PROJECT_NAME: 'cloud-project' } };
  const before = await fs.readFile(f.config.paths.credentials, 'utf8');
  assert.equal((await c.setup({ ...options, configureOnly: true, captureContent: false })).configured, true);
  const config = await c.resolveConfig(options);
  assert.equal(config.enabled, true); assert.equal(config.captureContent, false);
  assert.equal(await fs.readFile(f.config.paths.credentials, 'utf8'), before);
  await c.updateJson(config.paths.project, old => ({ ...old, captureContent: true }));
  assert.equal((await c.resolveConfig(options)).captureContent, false);
  // Restore the setup-owned hash before checking ownership-aware removal.
  await c.setup({ ...options, configureOnly: true, captureContent: false });
  await c.uninstall(options);
  for (const file of [config.paths.project, config.paths.user]) await assert.rejects(fs.stat(file), { code: 'ENOENT' });
  assert.equal(await fs.readFile(f.config.paths.credentials, 'utf8'), before);
});

test('uninstall does not remove a host directory that existed before setup', async t => {
  const f = await fixture(t); await c.uninstall(f.options);
  const host = path.join(f.home, 'existing-host'); await fs.mkdir(host, { mode: 0o700 });
  await f.setup({ agentDir: host });
  await c.uninstall({ ...f.options, agentDir: host });
  assert((await fs.stat(host)).isDirectory());
  assert.deepEqual(await fs.readdir(host), []);
});

test('Hermes native homes cannot inherit each others profile or activation', async t => {
  const f = await fixture(t, 'hermes');
  const other = { ...f.options, agentDir: path.join(f.home, 'other-native-home') };
  assert.equal((await c.resolveConfig(other)).enabled, false);
  const explicit = await c.resolveConfig({ ...other, env: { FI_API_KEY: 'native-synthetic-api', FI_SECRET_KEY: 'native-synthetic-secret', FI_OTLP_ENDPOINT: f.endpoint } });
  assert.equal(explicit.enabled, true);
  assert.equal(explicit.captureContent, true);
  assert.notEqual(c.deliveryStatePath(explicit), c.deliveryStatePath(f.config));
});

test('all lightweight compatibility entry points load without the exporter', () => {
  for (const entry of ['hook-state', 'collection', 'fence', 'control', 'config', 'files', 'privacy']) {
    execFileSync(process.execPath, ['-e', `require('@traceai/agent-core/${entry}'); if (Object.keys(require.cache).some(p => /sdk-trace|otlp-transformer/.test(p))) process.exit(1);`], { cwd: path.join(__dirname, '..') });
  }
});

test('all public entry points load as both CommonJS and ESM', () => {
  const pkg = require('../package.json');
  const entries = Object.keys(pkg.exports).map(key => key === '.' ? pkg.name : pkg.name + key.slice(1));
  execFileSync(process.execPath, ['--input-type=module', '-e', `for (const entry of ${JSON.stringify(entries)}) await import(entry);`], { cwd: path.join(__dirname, '..') });
  for (const entry of entries) assert(require(entry));
});

test('unresolved policy callbacks are bounded and cannot leak a queued span later', async t => {
  const f = await fixture(t);
  const processor = new c.BoundedSpanProcessor(f.config, undefined, () => new Promise(() => {}));
  const { BasicTracerProvider } = require('@opentelemetry/sdk-trace-base');
  const provider = new BasicTracerProvider({ spanProcessors: [processor] });
  provider.getTracer('test').startSpan('never-export').end();
  const before = Date.now();
  await processor.shutdown(100);
  assert(Date.now() - before < 1000);
  assert.equal(allSpans(f).length, 0);
});
