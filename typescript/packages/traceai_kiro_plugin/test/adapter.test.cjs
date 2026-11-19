const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { spawn } = require('node:child_process');
const core = require('@traceai/agent-core');
const { kiroAdapter, parseEvent, EVENTS } = require('../dist/index.js');
const { capture } = require('../dist/capture.js');
const attrs = s => Object.fromEntries((s.attributes ?? []).map(a => [a.key, Object.values(a.value)[0]]));
const spans = f => f.requests.flatMap(r => r.body.resourceSpans.flatMap(rs => rs.scopeSpans.flatMap(ss => ss.spans)));
const fixtures = () => fs.readFile(path.join(__dirname, 'fixtures/v3-schema-session.jsonl'), 'utf8').then(s => s.trim().split('\n').map(JSON.parse));
async function fixture(t, extra = {}) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'fi-kiro-'));
  const projectDir = path.join(home, 'project');
  await fs.mkdir(projectDir, { mode: 0o700 });
  const requests = [];
  const f = { home, projectDir, requests, code: 200 };
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', c => chunks.push(c));
    req.on('end', () => {
      const body = JSON.parse(Buffer.concat(chunks).toString());
      requests.push({ body, headers: req.headers });
      res.statusCode = f.code;
      res.setHeader('Content-Type', 'application/json');
      res.end('{}');
    });
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const endpoint = `http://127.0.0.1:${server.address().port}/tracer/v1/traces`;
  f.options = { agent: 'kiro-cli', home, projectDir, env: extra };
  const setup = await core.setup({ ...f.options, credentials: { apiKey: 'synthetic-api', secretKey: 'synthetic-secret' }, projectName: 'kiro-test', otlpEndpoint: endpoint, configureOnly: true });
  assert.equal(setup.configured, true);
  f.config = await core.resolveConfig(f.options);
  f.context = { config: f.config, scope: 'user', projectDir };
  f.env = { HOME: home, PATH: process.env.PATH, ...extra, FI_AGENT_CONFIG_DIR: f.config.paths.configDir, XDG_STATE_HOME: path.join(home, '.local/state') };
  t.after(async () => { server.closeAllConnections(); await new Promise(r => server.close(r)); await fs.rm(home, { recursive: true, force: true }); });
  return f;
}
async function event(f, name, body = {}) {
  const e = parseEvent(name, { session_id: 'schema-session', ...body }, f.config);
  if (e) await capture(e, f.config);
}
async function hook(f, name, body, command) {
  const args = [path.join(__dirname, '../dist/hook.cjs'), name, f.config.paths.configDir, path.dirname(f.config.paths.user)];
  const child = command ? spawn('/bin/sh', ['-c', command], { env: f.env, cwd: f.projectDir }) : spawn(process.execPath, args, { env: f.env, cwd: f.projectDir });
  let out = '', err = '';
  child.stdout.on('data', x => out += x); child.stderr.on('data', x => err += x);
  child.stdin.on('error', () => {});
  child.stdin.end(typeof body === 'string' ? body : JSON.stringify({ session_id: 'schema-session', hook_event_name: name, ...body, cwd: f.projectDir }));
  const code = await new Promise(r => child.on('close', r));
  assert.equal(code, 0); assert.equal(out, ''); assert.equal(err, '');
}
// Installer unit tests stub discovery only. test/real-kiro.cjs separately uses the actual binary.
const adapterForUnit = () => ({ ...kiroAdapter, detect: async () => ({ supported: true, version: '3.0.0' }) });

test('V3 schema fixture: two prompt roots/session, tools/errors; no fabricated LLM usage or duration', async t => {
  const f = await fixture(t);
  for (const r of await fixtures()) await hook(f, r.event, r.payload);
  await hook(f, 'UserPromptSubmit', { session_id: 'fixture-kiro-cli-session-0001', prompt: 'second prompt' });
  await hook(f, 'Stop', { session_id: 'fixture-kiro-cli-session-0001', assistant_response: 'second response' });
  await hook(f, 'Stop', { session_id: 'fixture-kiro-cli-session-0001' });
  const all = spans(f), roots = all.filter(s => !s.parentSpanId), tools = all.filter(s => attrs(s)['fi.span.kind'] === 'TOOL');
  assert.equal(all.length, 4); assert.equal(roots.length, 2); assert.equal(tools.length, 2);
  assert.notEqual(roots[0].traceId, roots[1].traceId);
  assert.equal(attrs(roots[0])['session.id'], attrs(roots[1])['session.id']);
  assert.ok(tools.every(s => s.parentSpanId === roots[0].spanId && s.startTimeUnixNano === s.endTimeUnixNano));
  assert.ok(tools.every(s => attrs(s)['fi.agent.timing'] === 'unknown'));
  assert.equal(tools.find(s => s.name === 'read_file').status.code, 2);
  assert.ok(all.every(s => attrs(s)['fi.span.kind'] !== 'LLM' && !Object.keys(attrs(s)).some(k => k.includes('usage.') && !k.startsWith('fi.agent') && !k.startsWith('fi.usage.'))));
  if (process.env.KIRO_EVIDENCE_DIR) await fs.writeFile(path.join(process.env.KIRO_EVIDENCE_DIR, 'schema-capture.json'), JSON.stringify({ provenance: 'schema-shaped fixtures, NOT real Kiro model execution', spans: all }, null, 2));
  const encoded = JSON.stringify(all);
  for (const m of ['PROMPT_MARKER', 'TOOL_OUTPUT_MARKER', 'ERROR_MARKER', 'RESPONSE_MARKER']) assert.ok(encoded.includes(m));
  for (const r of f.requests.filter(r => r.body.resourceSpans.length)) {
    assert.equal(r.headers['x-project-name'], 'kiro-test');
    assert.equal(attrs(r.body.resourceSpans[0].resource).project_type, 'observe');
  }
});

test('encrypted state, redaction before persistence, 0600 files, completion removes content', async t => {
  const f = await fixture(t);
  await event(f, 'UserPromptSubmit', { prompt: 'PROMPT_PRIVATE synthetic-api synthetic-secret' });
  const dir = path.join(f.config.paths.configDir, 'kiro-state'), name = (await fs.readdir(dir)).find(x => x.endsWith('.json'));
  const file = path.join(dir, name), raw = await fs.readFile(file, 'utf8');
  assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
  assert.ok(!raw.includes('PROMPT_PRIVATE') && !raw.includes('synthetic-secret'));
  const { createDecipheriv } = require('node:crypto');
  function decrypt(raw) { const e = JSON.parse(raw); const d = createDecipheriv('aes-256-gcm', Buffer.from(f.config.identityKey, 'hex'), Buffer.from(e.iv, 'base64')); d.setAAD(Buffer.from('futureagi-kiro-v1')); d.setAuthTag(Buffer.from(e.tag, 'base64')); return Buffer.concat([d.update(Buffer.from(e.data, 'base64')), d.final()]).toString(); }
  assert.ok(!decrypt(raw).includes('synthetic-secret'));
  assert.ok(decrypt(raw).includes('[REDACTED]'));
  await event(f, 'Stop');
  assert.ok(!decrypt(await fs.readFile(file, 'utf8')).includes('PROMPT_PRIVATE'));
});

test('privacy opt-out, error-detail suppression, missing ids and unknown/V2 events', async t => {
  const f = await fixture(t, { FI_AGENT_CAPTURE_CONTENT: 'false' });
  for (const r of await fixtures()) await hook(f, r.event, r.payload);
  assert.equal(spans(f).length, 3);
  assert.ok(!JSON.stringify(spans(f)).includes('MARKER'));
  assert.ok(!JSON.stringify(spans(f)).includes('/Users/fixture'));
  for (const name of ['userPromptSubmit', 'agentSpawn', 'PostFileSave', 'PreTaskExec', '']) assert.equal(parseEvent(name, { session_id: 's' }, f.config), undefined);
  assert.equal(parseEvent('Stop', {}, f.config), undefined);
  assert.equal(parseEvent('Stop', { session_id: 's', hook_event_name: 'PostToolUse' }, f.config), undefined);
  const hidden = { ...f.config, captureContent: true, hideOutputs: false, hideErrorDetails: true };
  assert.equal(parseEvent('PostToolUse', { session_id: 's', tool_response: { success: false, result: 'PRIVATE' } }, hidden).output, undefined);
});

test('PreToolUse never blocks or invents a call id; SessionEnd is interrupted, missing Stop is incomplete', async t => {
  const f = await fixture(t);
  await event(f, 'UserPromptSubmit', { prompt: 'one' });
  await hook(f, 'PreToolUse', { tool_name: 'read_file', tool_input: { path: 'public.txt' } });
  await event(f, 'UserPromptSubmit', { prompt: 'two' });
  await event(f, 'SessionEnd');
  assert.deepEqual(spans(f).map(s => attrs(s)['fi.agent.outcome']), ['incomplete', 'interrupted']);
});

test('disable fence and enable never replay old turn; destination change cannot reroute pending state', async t => {
  const f = await fixture(t);
  await event(f, 'UserPromptSubmit', { prompt: 'OLD_PROMPT' });
  await core.disable(f.options);
  await event(f, 'Stop');
  assert.equal(spans(f).length, 0);
  await core.enable(f.options);
  f.config = await core.resolveConfig(f.options);
  await event(f, 'Stop');
  assert.equal(spans(f).length, 0);
  await event(f, 'UserPromptSubmit', { prompt: 'OLD_DESTINATION' });
  f.config.projectName = 'new-destination';
  await event(f, 'Stop');
  assert.equal(spans(f).length, 0);
});

test('environment kill switch clears pending content before a later re-enable', async t => {
  const f = await fixture(t);
  await hook(f, 'UserPromptSubmit', { prompt: 'DO_NOT_REVIVE' });
  f.env.FI_AGENT_ENABLED = 'false';
  await hook(f, 'Stop', { assistant_response: 'DO_NOT_EXPORT' });
  assert.equal(spans(f).length, 0);
  delete f.env.FI_AGENT_ENABLED;
  await hook(f, 'Stop', {});
  assert.equal(spans(f).length, 0);
  await hook(f, 'UserPromptSubmit', { prompt: 'new allowed prompt' });
  await hook(f, 'Stop', {});
  assert.equal(spans(f).length, 1);
  assert.ok(!JSON.stringify(spans(f)).includes('DO_NOT_REVIVE'));
});

test('bounded content/state/tools, concurrent hooks, and no resend on 429', async t => {
  const f = await fixture(t);
  await event(f, 'UserPromptSubmit', { prompt: 'p'.repeat(40000) });
  for (let i = 0; i < 70; i++) await event(f, 'PostToolUse', { tool_name: 'read_file', tool_input: 'i'.repeat(20000), tool_response: { success: true, result: 'o'.repeat(20000) } });
  const dir = path.join(f.config.paths.configDir, 'kiro-state');
  const name = (await fs.readdir(dir)).find(x => x.endsWith('.json'));
  assert.ok((await fs.stat(path.join(dir, name))).size < 65536);
  f.code = 429;
  await Promise.all([event(f, 'Stop'), event(f, 'Stop')]);
  assert.equal(spans(f).length, 61);
  await event(f, 'Stop'); assert.equal(spans(f).length, 61);
  assert.equal((await core.inspectStatus(f.options)).delivery.lastError, 'RATE_LIMITED');
});

test('packaged executable is neutral with malformed/oversize input and unavailable exporter', async t => {
  const f = await fixture(t);
  await hook(f, 'Stop', '{bad');
  await hook(f, 'PreToolUse', 'x'.repeat(1024 * 1024 + 1));
  await hook(f, 'UserPromptSubmit', { prompt: 'network failure' });
  f.code = 503;
  await hook(f, 'Stop', {});
  assert.equal((await core.inspectStatus(f.options)).delivery.lastError, 'UNVERIFIED');
});

test('user-only standalone installer: persistent bundle, idempotence, PATH-independent hooks and ownership-safe uninstall', async t => {
  const f = await fixture(t), a = adapterForUnit();
  const unrelated = path.join(path.dirname(f.config.paths.user), 'hooks', 'unrelated.json');
  await fs.mkdir(path.dirname(unrelated), { recursive: true, mode: 0o700 });
  await fs.writeFile(unrelated, '{"version":"v1","hooks":[]}', { mode: 0o644 });
  assert.equal((await a.install(f.context)).registered, true);
  assert.equal((await a.install(f.context)).registered, true);
  const file = path.join(path.dirname(unrelated), 'futureagi-kiro-cli.json');
  const doc = JSON.parse(await fs.readFile(file, 'utf8'));
  assert.equal(doc.version, 'v1'); assert.deepEqual(doc.hooks.map(h => h.trigger), EVENTS);
  assert.ok(doc.hooks.every(h => h.timeout === 7 && !h.action.command.includes('npx') && !h.action.command.includes('/node_modules/')));
  assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
  f.env.PATH = '/nonexistent';
  await hook(f, 'UserPromptSubmit', { prompt: 'installed command' }, doc.hooks.find(h => h.trigger === 'UserPromptSubmit').action.command);
  await hook(f, 'Stop', { assistant_response: 'installed answer' }, doc.hooks.find(h => h.trigger === 'Stop').action.command);
  assert.equal(spans(f).length, 1);
  assert.equal((await a.install({ ...f.context, scope: 'project' })).registered, false);
  await fs.appendFile(file, ' ');
  assert.equal((await a.uninstall(f.context)).registered, true);
  assert.ok(await fs.readFile(file, 'utf8'));
  await fs.writeFile(file, JSON.stringify(doc, null, 2) + '\n');
  assert.equal((await a.uninstall(f.context)).registered, false);
  assert.equal(await fs.readFile(unrelated, 'utf8'), '{"version":"v1","hooks":[]}');
});

test('duplicates, unowned hook file and symlinks fail closed without changing unrelated settings', async t => {
  const f = await fixture(t), a = adapterForUnit();
  const dir = path.join(f.projectDir, '.kiro/hooks'); await fs.mkdir(dir, { recursive: true, mode: 0o700 });
  await fs.writeFile(path.join(dir, 'old.json'), '{"command":"python fi-hook-to-otlp/emit.py"}');
  assert.equal((await a.install(f.context)).duplicate, true);
  await fs.unlink(path.join(dir, 'old.json'));
  const host = path.join(path.dirname(f.config.paths.user), 'hooks'); await fs.mkdir(host, { mode: 0o700 });
  const target = path.join(host, 'futureagi-kiro-cli.json');
  await fs.writeFile(target, '{}', { mode: 0o600 });
  await assert.rejects(a.install(f.context), /HOOK_NOT_OWNED/);
  await fs.unlink(target); await fs.symlink(path.join(f.home, 'victim'), target);
  await assert.rejects(a.install(f.context));
});

test('Unicode/control-character content fits encrypted state and stale locks recover without stdout', async t => {
  const f = await fixture(t);
  await event(f, 'UserPromptSubmit', { prompt: '\u0000'.repeat(20000) });
  const dir = path.join(f.config.paths.configDir, 'kiro-state');
  const name = (await fs.readdir(dir)).find(x => x.endsWith('.json'));
  for (const suffix of ['.capture-lock', '.lock']) {
    const lock = path.join(dir, name + suffix);
    await fs.writeFile(lock, '', { mode: 0o600 });
    await fs.utimes(lock, new Date(0), new Date(0));
  }
  await event(f, 'PostToolUse', { tool_name: 'read_file', tool_response: '😀'.repeat(20000) });
  await event(f, 'Stop');
  assert.equal(spans(f).length, 2);
  assert.ok((await fs.stat(path.join(dir, name))).size < 65536);
});

test('privacy changes discard pending content instead of reviving it after re-enabling', async t => {
  const f = await fixture(t);
  await event(f, 'UserPromptSubmit', { prompt: 'OLD_CONTENT' });
  f.config = await core.resolveConfig({ ...f.options, env: { FI_AGENT_CAPTURE_CONTENT: 'false' } });
  await event(f, 'Stop');
  assert.equal(spans(f).length, 0);
  f.config = await core.resolveConfig(f.options);
  await event(f, 'Stop');
  assert.equal(spans(f).length, 0);
});

test('project override can reduce capture but cannot change credential destination; KIRO_HOME resolution', async t => {
  const f = await fixture(t);
  await core.writeProtected(f.config.paths.project, JSON.stringify({ schemaVersion: 1, captureContent: false }));
  assert.equal((await core.resolveConfig(f.options)).captureContent, false);
  await core.updateJson(f.config.paths.project, () => ({ schemaVersion: 1, transport: { endpoint: 'http://127.0.0.1:1/stolen' } }));
  assert.equal((await core.resolveConfig(f.options)).enabled, false);
  assert.equal(core.hostDirectory('kiro-cli', f.home, { KIRO_HOME: path.join(f.home, 'custom') }), path.join(f.home, 'custom'));
  for (const invalid of ['relative', '/', f.home, path.dirname(f.home)]) assert.equal(core.hostDirectory('kiro-cli', f.home, { KIRO_HOME: invalid }), path.join(f.home, '.kiro'));
});
