const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const { promisify } = require('node:util');
const execFile = promisify(require('node:child_process').execFile);
const core = require('@traceai/agent-core');
const { parseIdeEvent, IDE_EVENTS } = require('../dist/ide/events.js');
const { appendIdeEvent, routeId } = require('../dist/ide/state.js');
const { observeIdeEvent } = require('../dist/ide/observer.js');
const registration = require('../dist/ide/registration.js');
const { kiroIdeAdapter, IDE_QUALIFIED } = require('../dist/ide/index.js');
const attrs = span => Object.fromEntries((span.attributes || []).map(a => [a.key, Object.values(a.value)[0]]));

async function fixture(t, options = {}) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'kiro-ide-'));
  const home = path.join(root, 'home'), project = path.join(root, "workspace ' quoted");
  await fs.mkdir(home, { mode: 0o700 }); await fs.mkdir(project, { mode: 0o700 });
  const batches = [], headers = [];
  let responseStatus = options.status || 200;
  const server = http.createServer(async (req, res) => {
    let text = ''; for await (const b of req) text += b;
    const body = JSON.parse(text); if (body.resourceSpans?.length) { batches.push(body); headers.push(req.headers); }
    res.writeHead(responseStatus, { 'Content-Type': 'application/json' }); res.end('{}');
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(async () => { await new Promise(r => server.close(r)); await fs.rm(root, { recursive: true, force: true }); });
  const env = { HOME: home, XDG_STATE_HOME: path.join(home, 'state'), XDG_CONFIG_HOME: path.join(home, 'config'),
    FI_AGENT_CONFIG_DIR: path.join(home, 'config', 'futureagi', 'agents'), ...options.env };
  const opts = { agent: 'kiro-ide', home, env, projectDir: project };
  const endpoint = `http://127.0.0.1:${server.address().port}/tracer/v1/traces`;
  if (!options.status) {
    const setup = await core.setup({ ...opts, projectName: 'test-ide', otlpEndpoint: endpoint,
      credentials: { apiKey: 'dummy-ide-api', secretKey: 'dummy-ide-secret' }, configureOnly: true });
    assert.equal(setup.configured, true);
  }
  const config = await core.resolveConfig(opts);
  return { root, home, project, env, opts, endpoint, config, context: { config, scope: 'project', projectDir: project }, batches, headers,
    setStatus: value => { responseStatus = value; },
    spans: () => batches.flatMap(b => b.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans))) };
}
const payload = (event, fields = {}) => ({ hook_event_name: event, session_id: 'ide-session', ...fields });
async function emit(f, event, fields = {}, config = f.config) { await observeIdeEvent(config, event, payload(event, fields)); }

test('IDE support is held independently of CLI and no permission override can qualify it', async t => {
  const f = await fixture(t);
  assert.equal(IDE_QUALIFIED, false);
  assert.equal((await kiroIdeAdapter.detect(f.context)).supported, false);
  await assert.rejects(kiroIdeAdapter.install(f.context), /GUI_AND_PAYLOAD/);
  assert(!IDE_EVENTS.includes('SessionEnd'));
  assert(IDE_EVENTS.includes('PreTaskExec'));
});

test('concurrent new IDE sessions initialize one recovery key without dropping prompts', async t => {
  const f = await fixture(t);
  const sessions = ['one', 'two', 'three', 'four'];
  await Promise.all(sessions.map(session_id => emit(f, 'UserPromptSubmit', { session_id, prompt: 'public' })));
  await Promise.all(sessions.map(session_id => emit(f, 'Stop', { session_id })));
  assert.equal(f.spans().filter(s => !s.parentSpanId).length, sessions.length);
});

test('parser requires explicit session identity; never adopts CLI aliases or arbitrary fields', async t => {
  const f = await fixture(t);
  assert.equal(parseIdeEvent('UserPromptSubmit', { prompt: 'hello', cwd: f.project }, f.config), undefined);
  assert.equal(parseIdeEvent('userPromptSubmit', payload('userPromptSubmit'), f.config), undefined);
  assert.equal(parseIdeEvent('Stop', payload('PostToolUse'), f.config), undefined);
  const event = parseIdeEvent('UserPromptSubmit', payload('UserPromptSubmit', { cwd: '/sensitive', transcript_path: '/secret', turn_id: 'not-in-ide-schema' }), f.config, 'documented USER_PROMPT');
  assert.equal(event.input, 'documented USER_PROMPT');
  assert.equal(event.cwd, undefined); assert.equal(event.transcript_path, undefined); assert.equal(event.turn, undefined);
  const stop = parseIdeEvent('Stop', payload('Stop', { assistant_response: 'not-in-ide-schema' }), f.config);
  assert.equal(stop.output, undefined);
  const tool = parseIdeEvent('PostToolUse', payload('PostToolUse', { tool_use_id: 'not-in-ide-schema' }), f.config);
  assert.equal(tool.callId, undefined);
});

test('two fixture turns produce two roots in one session; tools and notifications never become model requests', async t => {
  const f = await fixture(t);
  for (const turn_id of ['first', 'second']) {
    await emit(f, 'UserPromptSubmit', { turn_id, prompt: 'inspect dummy-ide-secret' });
    await emit(f, 'PreToolUse', { turn_id, tool_name: 'shell' });
    await emit(f, 'PostToolUse', { turn_id, tool_name: 'shell', tool_use_id: `${turn_id}-tool`, tool_input: { command: 'echo hello' }, tool_response: { success: true, result: 'hello' } });
    await emit(f, 'PostFileSave', { turn_id, file_path: 'public/example.ts' });
    await emit(f, 'PreTaskExec', { turn_id }); await emit(f, 'PostTaskExec', { turn_id });
    await emit(f, 'PreCompact', { reason: 'context-limit', transcript_path: '/never-read' });
    await emit(f, 'PostCompact', { reason: 'context-limit', summary: 'not-a-generation' });
    await emit(f, 'Stop', { turn_id, assistant_response: 'not-in-ide-schema', tokens: 99999 });
  }
  const spans = f.spans(), roots = spans.filter(s => !s.parentSpanId), tools = spans.filter(s => attrs(s)['fi.span.kind'] === 'TOOL');
  assert.equal(roots.length, 2); assert.equal(tools.length, 2);
  assert.equal(new Set(roots.map(s => attrs(s)['session.id'])).size, 1);
  for (const tool of tools) {
    assert(roots.some(r => r.spanId === tool.parentSpanId && r.traceId === tool.traceId));
    assert.equal(tool.startTimeUnixNano, tool.endTimeUnixNano);
    assert.equal(attrs(tool)['fi.agent.timing'], 'observed-event');
  }
  assert.deepEqual(roots[0].events.map(e => e.name), ['file.saved', 'task.started', 'task.finished', 'compaction.started', 'compaction.finished']);
  assert.equal(attrs(roots[0])['fi.agent.timing'], 'observed-event');
  assert.equal(attrs(roots[0])['fi.agent.outcome'], 'incomplete');
  const raw = JSON.stringify(f.batches);
  assert(!raw.includes('dummy-ide-secret')); assert(!raw.includes('/never-read')); assert(raw.includes('public/example.ts'));
  assert(!raw.includes('not-in-ide-schema')); assert(!raw.includes('not-a-generation'));
  assert(!raw.includes('token_count')); assert(!raw.includes('usage.cost')); assert(!raw.includes('"LLM"'));
  assert(f.headers.every(h => h['x-api-key'] === 'dummy-ide-api' && h['x-secret-key'] === 'dummy-ide-secret' && h['x-project-name'] === 'test-ide'));
  const resource = f.batches[0].resourceSpans[0].resource.attributes;
  assert(resource.some(a => a.key === 'project_type' && a.value.stringValue === 'observe'));
  const status = await core.inspectStatus(f.opts);
  assert.equal(status.delivery.exported, 4);
  assert(status.delivery.lastAcceptedAt);
});

test('state is encrypted, owner-only, terminally claimed and duplicate stop never exports twice', async t => {
  const f = await fixture(t);
  await emit(f, 'UserPromptSubmit', { prompt: 'private-content', turn_id: 'one' });
  const dir = path.join(f.config.paths.stateDir, 'kiro-ide-turns');
  const [name] = (await fs.readdir(dir)).filter(n => n.endsWith('.json'));
  const file = path.join(dir, name);
  assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
  assert(!(await fs.readFile(file, 'utf8')).includes('private-content'));
  await emit(f, 'Stop', { turn_id: 'one' });
  assert.equal(JSON.parse(await fs.readFile(file, 'utf8')).sealed, undefined);
  await emit(f, 'Stop', { turn_id: 'one' });
  assert.equal(f.spans().length, 1);
});

test('current event leases wait, legacy claims are inert, and stop still exports once', async t => {
  const f = await fixture(t);
  await emit(f, 'UserPromptSubmit', { turn_id: 'claim' });
  const dir = path.join(f.config.paths.stateDir, 'kiro-ide-turns');
  const name = (await fs.readdir(dir)).find(n => n.endsWith('.json'));
  await fs.writeFile(path.join(dir, name + '.claim'), '', { mode: 0o600 });
  const { acquireWriteLock } = require('@traceai/agent-core/file-lock');
  const lease = await acquireWriteLock(path.join(dir, name + '.event'));
  const release = new Promise((resolve, reject) => setTimeout(() => lease.release().then(resolve, reject), 100));
  await emit(f, 'Stop', { turn_id: 'claim' });
  await release;
  assert.equal(f.spans().length, 1);
  await emit(f, 'Stop', { turn_id: 'claim' }); assert.equal(f.spans().length, 1);
});

test('structure-only privacy applies before persistence; error hide is stricter than content-on', async t => {
  const f = await fixture(t, { env: { FI_AGENT_CAPTURE_CONTENT: 'false' } });
  assert.equal(f.config.captureContent, false);
  const event = parseIdeEvent('PostToolUse', payload('PostToolUse', { tool_name: 'shell', tool_input: 'secret input', tool_response: { success: false, text: 'secret output' } }), f.config);
  assert.equal(event.input, undefined); assert.equal(event.output, undefined); assert.equal(event.failed, true);
  const strict = { ...f.config, captureContent: true, hideInputs: false, hideOutputs: false, hideErrorDetails: true };
  assert.equal(parseIdeEvent('PostToolUse', payload('PostToolUse', { tool_response: { success: false, text: 'hidden' } }), strict).output, undefined);
  await emit(f, 'UserPromptSubmit', { prompt: 'omit-this' });
  await emit(f, 'Stop', { assistant_response: 'omit-that' });
  assert(!JSON.stringify(f.batches).includes('omit-'));
});

test('disable fences old config and re-enable cannot send an old turn', async t => {
  const f = await fixture(t);
  await emit(f, 'UserPromptSubmit', { turn_id: 'old', prompt: 'old' });
  await core.disable(f.opts);
  await emit(f, 'Stop', { turn_id: 'old' });
  assert.equal(f.spans().length, 0);
  await core.enable(f.opts);
  const next = await core.resolveConfig(f.opts);
  assert.notEqual(routeId(next), routeId(f.config));
  await emit(f, 'Stop', { turn_id: 'old' }, next);
  assert.equal(f.spans().length, 0);
  await emit(f, 'UserPromptSubmit', { turn_id: 'new' }, next); await emit(f, 'Stop', { turn_id: 'new' }, next);
  assert.equal(f.spans().length, 1);
});

test('missing final boundary is not inferred and changed route drops late events', async t => {
  const f = await fixture(t);
  await emit(f, 'UserPromptSubmit', { prompt: 'a' });
  await emit(f, 'PostFileSave', { file_path: 'a.ts' });
  assert.equal(f.spans().length, 0);
  const changed = { ...f.config, projectName: 'other', credentials: f.config.credentials };
  await emit(f, 'Stop', { turn_id: 'a' }, changed);
  assert.equal(f.spans().length, 0);
});

test('fixture state is bounded without inventing source call identity', async t => {
  const f = await fixture(t);
  await emit(f, 'UserPromptSubmit', { turn_id: 'limit', prompt: 'x'.repeat(30000) });
  for (let i = 0; i < 70; i++) await emit(f, 'PostToolUse', { turn_id: 'limit', tool_use_id: `t-${i}`, tool_name: 'read', tool_response: 'x'.repeat(25000) });
  await emit(f, 'Stop', { turn_id: 'limit' });
  assert(f.spans().length <= 63);
  const root = f.spans().find(s => !s.parentSpanId);
  assert(root.events.some(e => e.name.startsWith('gap.')));
});

test('export failure is bounded, diagnosed, and terminal content is not retried', async t => {
  const f = await fixture(t); f.setStatus(503);
  await emit(f, 'UserPromptSubmit', { turn_id: 'outage' });
  const start = Date.now(); await emit(f, 'Stop', { turn_id: 'outage' });
  assert(Date.now() - start < 6000);
  const status = await core.inspectStatus(f.opts);
  assert.equal(status.delivery.exported, 0); assert.equal(status.delivery.dropped, 1);
  assert(status.delivery.lastError);
  f.setStatus(200); await emit(f, 'Stop', { turn_id: 'outage' });
  assert.equal(f.batches.length, 1);
});

test('core span events obey redaction, content privacy, bounds and terminal fencing', async t => {
  const f = await fixture(t), tracer = core.createTurnTracer({ config: f.config });
  const turn = tracer.startTurn({ sessionId: 'events', turnId: 'first' });
  turn.event({ name: 'dummy-ide-secret' });
  turn.event({ name: 'invalid event name' });
  turn.event({ name: 'file.saved', input: 'contains dummy-ide-api' });
  for (let i = 0; i < 70; i++) turn.event({ name: 'file.saved' });
  turn.end(); turn.event({ name: 'after.end' });
  await tracer.flush(); await tracer.shutdown();
  assert.equal(f.spans()[0].events.length, 64);
  assert(!JSON.stringify(f.batches).includes('dummy-ide-api'));
  assert(!JSON.stringify(f.batches).includes('dummy-ide-secret'));
  assert(!JSON.stringify(f.batches).includes('after.end'));
  const privateConfig = await core.resolveConfig({ ...f.opts, env: { ...f.env, FI_AGENT_CAPTURE_CONTENT: 'false' } });
  const second = core.createTurnTracer({ config: privateConfig });
  const hidden = second.startTurn({ sessionId: 'events', turnId: 'second' });
  hidden.event({ name: 'file.saved', input: 'do-not-save' }); hidden.end();
  await second.flush(); await second.shutdown();
  assert(!JSON.stringify(f.batches).includes('do-not-save'));
});

test('manifest is a versioned IDE workspace candidate, not a power or agent prompt', async () => {
  const doc = JSON.parse(registration.ideManifest('/persistent runtime', '/private config', "/project ' quoted"));
  assert.equal(doc.version, 'v1'); assert.equal(doc.hooks.length, IDE_EVENTS.length);
  assert(doc.hooks.every(h => h.action.type === 'command' && !h.action.prompt && h.action.command.includes('|| :')));
  assert(doc.hooks.every(h => h.timeout <= 8));
  assert(!JSON.stringify(doc).includes('npx')); assert(!JSON.stringify(doc).includes('api-key'));
});

test('owned standalone registration is repeatable and preserves unrelated hooks; uninstall is scoped', async t => {
  const f = await fixture(t);
  const hookdir = path.join(f.project, '.kiro', 'hooks'); await fs.mkdir(hookdir, { recursive: true, mode: 0o700 });
  const foreign = path.join(hookdir, 'my-lint.json'), text = '{ "version":"v1", "hooks": [] }';
  await fs.writeFile(foreign, text, { mode: 0o644 });
  const runtime = await registration.installIdeRuntime(f.context);
  assert(!runtime.includes('node_modules'));
  assert.equal((await fs.lstat(path.join(runtime, 'node'))).isSymbolicLink(), true);
  assert.equal(await fs.realpath(path.join(runtime, 'node')), await fs.realpath(process.execPath));
  assert.equal(await registration.installIdeRuntime(f.context), runtime);
  const first = await registration.registerIde(f.context, runtime), second = await registration.registerIde(f.context, runtime);
  assert.equal(first.registered, true); assert.equal(second.registered, true); assert.equal(second.activated, false); assert.equal(second.trust, 'required');
  assert.equal(await fs.readFile(foreign, 'utf8'), text);
  await registration.unregisterIde(f.context);
  await registration.unregisterIde(f.context);
  assert.equal(await fs.readFile(foreign, 'utf8'), text);
  assert.equal((await registration.inspectIde(f.context)).registered, false);
});

test('modified owned hook, competing integration and symlink are never overwritten', async t => {
  const f = await fixture(t), runtime = '/persistent/kiro';
  await registration.registerIde(f.context, runtime);
  const file = path.join(f.project, '.kiro', 'hooks', 'futureagi-ide.json');
  await fs.writeFile(file, '{"user":"edited"}');
  await assert.rejects(registration.registerIde(f.context, runtime), /OWNED_HOOK_MODIFIED/);
  await assert.rejects(registration.unregisterIde(f.context), /OWNED_HOOK_MODIFIED/);
  await fs.unlink(file); await fs.symlink(path.join(f.root, 'victim'), file);
  await assert.rejects(registration.registerIde(f.context, runtime));
  await fs.unlink(file);
  await fs.writeFile(path.join(path.dirname(file), 'other.json'), '{"command":"fi-kiro-hook"}', { mode: 0o600 });
  await assert.rejects(registration.registerIde(f.context, runtime), /DUPLICATE_KIRO_REGISTRATION/);
});

test('packaged hook is neutral with stripped PATH, malformed input, killed tracing, and missing config', async t => {
  const f = await fixture(t);
  const runtime = await registration.installIdeRuntime(f.context);
  const command = JSON.parse(registration.ideManifest(runtime, f.config.paths.configDir, f.project)).hooks.find(h => h.trigger === 'Stop').action.command;
  const result = await execFile('/bin/sh', ['-c', command], { env: { HOME: f.home, PATH: '/absent', USER_PROMPT: 'must-not-export', FI_AGENT_ENABLED: 'false' }, timeout: 3000 });
  assert.equal(result.stdout, ''); assert.equal(result.stderr, ''); assert.equal(f.spans().length, 0);
  const direct = await execFile(path.join(runtime, 'node'), [path.join(runtime, 'ide-hook.cjs'), '--event', 'Stop'], { env: { HOME: f.home, PATH: '/absent' }, timeout: 3000 });
  assert.equal(direct.stdout, ''); assert.equal(direct.stderr, '');
});

test('unified setup explains HOLD before requesting keys or making any files', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'kiro-cli-gate-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const cli = path.resolve(__dirname, '../../traceai_agents/dist/cli.js');
  let result; try { await execFile(process.execPath, [cli, 'setup', 'kiro-ide', '--project', 'coding-agents', '--json'], { env: { HOME: root, FI_AGENT_CONFIG_DIR: path.join(root, 'config') }, cwd: root }); }
  catch (e) { result = e; }
  assert.equal(result.code, 2); assert.match(result.stdout, /GUI_AND_PAYLOAD_QUALIFICATION_REQUIRED/);
  assert.equal(result.stderr, ''); assert.deepEqual(await fs.readdir(root), []);
});
