const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const { spawn } = require('node:child_process');
const core = require('@traceai/agent-core');
const { augmentAdapter, scriptCommand, EVENTS } = require('../dist/adapter.js');
const { normalize } = require('../dist/events.js');
const { handlePayload } = require('../dist/hook.js');
const state = require('../dist/state.js');
const { parse } = require('jsonc-parser');

async function fixture(t, captureContent = true, homeName = 'home') {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'fi-augment-'));
  const home = path.join(root, homeName), projectDir = path.join(root, 'project');
  await fs.mkdir(home, { recursive: true, mode: 0o700 });
  await fs.mkdir(projectDir, { mode: 0o700 });
  const received = [];
  let responseCode = 200;
  const server = http.createServer((req, res) => {
    let data = '';
    req.on('data', (b) => data += b);
    req.on('end', () => {
      received.push({ headers: req.headers, body: JSON.parse(data) });
      res.writeHead(req.headers['x-api-key'] === 'synthetic-api' ? responseCode : 401, { 'content-type': 'application/json' });
      res.end('{}');
    });
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const env = { HOME: home, XDG_CONFIG_HOME: path.join(home, '.config'), XDG_STATE_HOME: path.join(home, '.state'),
    FI_API_KEY: 'synthetic-api', FI_SECRET_KEY: 'synthetic-secret', FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/tracer/v1/traces` };
  const options = { agent: 'augment', home, env, projectDir };
  const result = await core.setup({ ...options, projectName: 'fixture-project', captureContent, configureOnly: true });
  assert.equal(result.configured, true);
  const config = await core.resolveConfig(options);
  const context = { config, projectDir, scope: 'user' };
  t.after(async () => { await new Promise((r) => server.close(r)); await fs.rm(root, { recursive: true, force: true }); });
  return { root, home, projectDir, options, env, config, context, received, setCode: (n) => responseCode = n,
    spans: () => received.flatMap((r) => r.body.resourceSpans ?? []).flatMap((r) => r.scopeSpans ?? []).flatMap((s) => s.spans ?? []) };
}
const attrs = (span) => Object.fromEntries(span.attributes.map((a) => [a.key, a.value.stringValue ?? a.value.boolValue ?? a.value.intValue]));
const body = (event, extra = {}) => ({ hook_event_name: event, conversation_id: 'conversation', workspace_roots: ['/never-read-this'], ...extra });
async function replay(f) {
  const events = (await fs.readFile(path.join(__dirname, 'fixtures/schema-session.jsonl'), 'utf8')).trim().split('\n').map(JSON.parse);
  for (const e of events) await handlePayload(e.payload, f.config);
}
function run(file, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(file, args, { ...options, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    child.stdout.on('data', (b) => stdout += b); child.stderr.on('data', (b) => stderr += b);
    child.on('error', reject); child.on('close', (code) => resolve({ code, stdout, stderr }));
    child.stdin.end(options.input ?? '{}');
  });
}

// Schema-shaped fixtures are not a real-agent claim.
test('Stop summaries group actual tools, preserve errors, and never invent LLM usage', async (t) => {
  const f = await fixture(t); await replay(f);
  const spans = f.spans(), roots = spans.filter((s) => !s.parentSpanId);
  assert.equal(roots.length, 2); assert.equal(spans.length, 7);
  assert.equal(new Set(roots.map((s) => attrs(s)['session.id'])).size, 1);
  assert.equal(new Set(roots.map((s) => s.traceId)).size, 2);
  assert.ok(roots.every((s) => attrs(s)['fi.agent.unit'] === 'stop-summary'));
  assert.ok(spans.every((s) => attrs(s)['fi.span.kind'] !== 'LLM'));
  assert.ok(spans.every((s) => !Object.keys(attrs(s)).some((k) => /token_count|usage.cost/.test(k))));
  assert.equal(spans.filter((s) => s.status.code === 2).length, 2);
  for (const s of spans.filter((s) => s.parentSpanId)) assert.ok(roots.some((r) => s.parentSpanId === r.spanId && s.traceId === r.traceId));
  const output = JSON.stringify(f.received);
  assert.match(output, /CONVERSATION_CANARY prompt/); assert.match(output, /TOOL_OUTPUT_MARKER_AUG/);
  assert.match(output, /FILE_CONTENT_CANARY/);
  assert.doesNotMatch(output, /fixture.user@example.com|MODEL_LABEL_CANARY|EXCHANGE_CANARY|7319|PROMPT_CANARY_AUG/);
  for (const req of f.received) assert.equal(req.headers['x-project-name'], 'fixture-project');
});
test('structure-only mode removes all content including errors and file changes', async (t) => {
  const f = await fixture(t, false); await replay(f);
  assert.equal(f.spans().length, 7);
  assert.doesNotMatch(JSON.stringify(f.spans()), /CANARY|MARKER|input.value|output.value|fixture.user@example.com|workspace_roots/);
  assert.equal(f.spans().filter((s) => s.status.code === 2).length, 2);
});
test('known credentials redacted before encrypted persistence and export', async (t) => {
  const f = await fixture(t);
  await handlePayload(body('PostToolUse', { tool_name: 'view', tool_input: 'synthetic-api', tool_output: 'synthetic-secret' }), f.config);
  const p = state.statePath(f.config, 'conversation'), saved = await state.load(p, f.config);
  assert.doesNotMatch(JSON.stringify(saved), /synthetic-api|synthetic-secret/);
  assert.match(saved.tools[0].input, /REDACTED/);
  assert.equal((await fs.stat(p)).mode & 0o777, 0o600);
  assert.doesNotMatch(await fs.readFile(p, 'utf8'), /REDACTED|view|synthetic/);
});
test('stop-only conversation has one summary, interruption stays interrupted', async (t) => {
  const f = await fixture(t);
  await handlePayload(body('Stop', { agent_stop_cause: 'interrupted', conversation: { userPrompt: 'one', agentTextResponse: 'partial' } }), f.config);
  assert.equal(f.spans().length, 1); assert.equal(attrs(f.spans()[0])['fi.agent.outcome'], 'interrupted');
});
test('SessionEnd flushes pending tools as incomplete, no following prompt required', async (t) => {
  const f = await fixture(t);
  await handlePayload(body('PostToolUse', { tool_name: 'view', tool_output: 'x' }), f.config);
  await handlePayload(body('SessionEnd'), f.config);
  assert.equal(f.spans().length, 2); assert.equal(attrs(f.spans().find((s) => !s.parentSpanId))['fi.agent.outcome'], 'incomplete');
});
test('privacy reduction scrubs previously buffered content', async (t) => {
  const f = await fixture(t);
  await handlePayload(body('PostToolUse', { tool_name: 'view', tool_output: 'BUFFERED_CANARY' }), f.config);
  const config = await core.resolveConfig({ ...f.options, env: { ...f.env, FI_AGENT_CAPTURE_CONTENT: 'false' } });
  await handlePayload(body('Stop', { agent_stop_cause: 'end_turn' }), config);
  assert.doesNotMatch(JSON.stringify(f.spans()), /BUFFERED_CANARY/);
});
test('persistent disable fences stale config and re-enable cannot resurrect state', async (t) => {
  const f = await fixture(t);
  await handlePayload(body('PostToolUse', { tool_name: 'view', tool_output: 'OLD_CANARY' }), f.config);
  await core.disable(f.options);
  await handlePayload(body('Stop', { agent_stop_cause: 'end_turn' }), f.config);
  assert.equal(f.spans().length, 0);
  await core.enable(f.options);
  const config = await core.resolveConfig(f.options);
  await handlePayload(body('Stop', { agent_stop_cause: 'end_turn' }), config);
  assert.doesNotMatch(JSON.stringify(f.spans()), /OLD_CANARY/);
});
test('state capacity is bounded and loss is recorded', async (t) => {
  const f = await fixture(t);
  for (let i = 0; i < 36; i++) await handlePayload(body('PostToolUse', { tool_name: 'view', tool_output: 'x'.repeat(20000) }), f.config);
  const p = state.statePath(f.config, 'conversation');
  assert.ok((await fs.stat(p)).size < 65536);
  const saved = await state.load(p, f.config);
  assert.ok(saved.dropped > 0);
  await handlePayload(body('Stop', { agent_stop_cause: 'end_turn' }), f.config);
  assert.equal(attrs(f.spans().find((s) => !s.parentSpanId))['fi.agent.outcome'], 'incomplete');
});
test('unknown, PreToolUse and missing session are ignored; metadata never harvested', async (t) => {
  const f = await fixture(t);
  assert.equal(normalize(body('PreToolUse'), f.config), undefined);
  assert.equal(normalize({ hook_event_name: 'Stop' }, f.config), undefined);
  assert.equal(normalize(body('Notification'), f.config), undefined);
  const x = normalize(body('Stop', { context: { userEmail: 'person@example.com' }, _exchange: { tokens: 123 } }), f.config);
  assert.doesNotMatch(JSON.stringify(x), /person|tokens/);
});
test('installer merges JSONC, is idempotent, timeout is ms, metadata on Stop only', async (t) => {
  const f = await fixture(t);
  const file = path.join(f.home, '.augment/settings.json');
  await fs.writeFile(file, '{\n  // Keep this preference\n  "theme": "dark",\n  "hooks": {"Stop": [{"hooks":[{"type":"command","command":"/existing.sh"}]}]}\n}', { mode: 0o644 });
  assert.equal((await augmentAdapter.install(f.context)).registered, true);
  assert.equal((await augmentAdapter.install(f.context)).registered, true);
  const raw = await fs.readFile(file, 'utf8'), settings = parse(raw);
  assert.match(raw, /Keep this preference/); assert.equal(settings.theme, 'dark');
  assert.equal(settings.hooks.Stop.length, 2);
  for (const event of EVENTS) {
    const group = settings.hooks[event].find((g) => g.hooks[0].command.includes('futureagi-hook.sh'));
    assert.equal(group.hooks[0].timeout, 7000);
    assert.deepEqual(group.metadata, { includeConversationData: event === 'Stop', includeUserContext: false, includeMCPMetadata: false });
  }
  assert.equal(settings.hooks.PreToolUse, undefined);
  await augmentAdapter.uninstall(f.context);
  const removed = parse(await fs.readFile(file, 'utf8'));
  assert.equal(removed.hooks.Stop.length, 1); assert.equal(removed.hooks.Stop[0].hooks[0].command, '/existing.sh');
});
test('version upgrade reconciles stable ownership instead of creating duplicate hooks', async (t) => {
  const f = await fixture(t); await augmentAdapter.install(f.context);
  const registrations = path.join(f.config.paths.configDir, 'registrations/augment');
  const ownerFile = path.join(registrations, (await fs.readdir(registrations))[0]);
  const owner = JSON.parse(await fs.readFile(ownerFile, 'utf8'));
  const settingsFile = path.join(f.home, '.augment/settings.json');
  const doc = parse(await fs.readFile(settingsFile, 'utf8'));
  for (const event of EVENTS) {
    const command = owner.groups[event].hooks[0].command.replace('/0.1.0/', '/0.0.9/');
    owner.groups[event].hooks[0].command = command;
    doc.hooks[event][0].hooks[0].command = command;
  }
  await fs.writeFile(ownerFile, JSON.stringify(owner));
  await fs.writeFile(settingsFile, JSON.stringify(doc));
  assert.equal((await augmentAdapter.install(f.context)).registered, true);
  const updated = parse(await fs.readFile(settingsFile, 'utf8'));
  assert.ok(EVENTS.every((event) => updated.hooks[event].length === 1));
  assert.doesNotMatch(JSON.stringify(updated), /0\.0\.9/);
});
test('stricter error-detail setting removes error text without hiding ordinary outputs', async (t) => {
  const f = await fixture(t);
  const config = await core.resolveConfig({ ...f.options, env: { ...f.env, FI_HIDE_ERROR_DETAILS: 'true' } });
  await handlePayload(body('PostToolUse', { tool_name: 'view', tool_error: 'PRIVATE_ERROR' }), config);
  await handlePayload(body('PostToolUse', { tool_name: 'view', tool_output: 'SAFE_OUTPUT' }), config);
  await handlePayload(body('Stop', { agent_stop_cause: 'end_turn' }), config);
  assert.doesNotMatch(JSON.stringify(f.spans()), /PRIVATE_ERROR/);
  assert.match(JSON.stringify(f.spans()), /SAFE_OUTPUT/);
});
test('capture-off registration does not request conversation data', async (t) => {
  const f = await fixture(t, false); await augmentAdapter.install(f.context);
  const settings = parse(await fs.readFile(path.join(f.home, '.augment/settings.json'), 'utf8'));
  assert.equal(settings.hooks.Stop[0].metadata.includeConversationData, false);
});
test('project scope keeps source scripts and secrets outside the repository', async (t) => {
  const f = await fixture(t);
  const c = { ...f.context, scope: 'project' };
  await augmentAdapter.install(c);
  const text = await fs.readFile(path.join(f.projectDir, '.augment/settings.local.json'), 'utf8');
  assert.doesNotMatch(text, /synthetic-api|synthetic-secret/);
  assert.match(text, /\.config/);
  assert.deepEqual(await fs.readdir(path.join(f.projectDir, '.augment')), ['settings.local.json']);
  await assert.rejects(augmentAdapter.install(f.context), /DUPLICATE_AUGMENT_REGISTRATION/);
});
test('modified owned hook is never overwritten or uninstalled', async (t) => {
  const f = await fixture(t); await augmentAdapter.install(f.context);
  const file = path.join(f.home, '.augment/settings.json'), doc = parse(await fs.readFile(file, 'utf8'));
  doc.hooks.Stop[0].hooks[0].timeout = 99;
  await fs.writeFile(file, JSON.stringify(doc));
  await assert.rejects(augmentAdapter.install(f.context), /AUGMENT_HOOK_CONFLICT/);
  await assert.rejects(augmentAdapter.uninstall(f.context), /AUGMENT_HOOK_CONFLICT/);
  assert.equal(parse(await fs.readFile(file, 'utf8')).hooks.Stop[0].hooks[0].timeout, 99);
});
test('symlink settings and malformed hooks are rejected without changing target', async (t) => {
  const f = await fixture(t);
  const file = path.join(f.home, '.augment/settings.json'), target = path.join(f.home, 'target');
  await fs.writeFile(target, '{}'); await fs.symlink(target, file);
  await assert.rejects(augmentAdapter.install(f.context));
  assert.equal(await fs.readFile(target, 'utf8'), '{}');
  await fs.unlink(file); await fs.writeFile(file, '{"hooks": []}');
  await assert.rejects(augmentAdapter.install(f.context), /INVALID_AUGMENT_HOOKS/);
});
test('encrypted state is destination-bound and tampering fails closed', async (t) => {
  const f = await fixture(t);
  await handlePayload(body('PostToolUse', { tool_name: 'view', tool_output: 'BOUND_CANARY' }), f.config);
  const file = state.statePath(f.config, 'conversation');
  const env = { ...f.env, FI_PROJECT_NAME: 'other' }, other = await core.resolveConfig({ ...f.options, env });
  assert.notEqual(state.statePath(other, 'conversation'), file);
  assert.equal((await state.load(file, other)).tools.length, 0);
  await fs.writeFile(file, '{"data":"tamper"}');
  assert.equal((await state.load(file, f.config)).tools.length, 0);
});
test('exporter rejection drops once, records sanitized status, never retries', async (t) => {
  const f = await fixture(t); f.setCode(429);
  await handlePayload(body('Stop', { agent_stop_cause: 'end_turn' }), f.config);
  assert.equal(f.received.length, 2); // setup validation + one export
  const status = await core.inspectStatus({ ...f.options, adapter: augmentAdapter });
  assert.match(JSON.stringify(status), /RATE_LIMITED/);
  assert.doesNotMatch(JSON.stringify(status), /synthetic-api|synthetic-secret/);
});
for (const name of ['space home', 'José', 'name(test)', "quote'home", 'literal $HOME & `id`']) {
  test(`packaged launcher is PATH-independent and neutral in ${name}`, async (t) => {
    const f = await fixture(t, true, name); await augmentAdapter.install(f.context);
    const settings = parse(await fs.readFile(path.join(f.home, '.augment/settings.json'), 'utf8'));
    const command = settings.hooks.Stop[0].hooks[0].command;
    const options = { cwd: f.projectDir, env: { HOME: f.home, PATH: '', AUGMENT_PROJECT_DIR: f.projectDir },
      input: JSON.stringify(body('Stop', { agent_stop_cause: 'end_turn', conversation: { userPrompt: 'PACKED_PROMPT', agentTextResponse: 'PACKED_REPLY' } })) };
    const result = await run('/bin/sh', ['-c', command], options);
    assert.deepEqual(result, { code: 0, stdout: '', stderr: '' });
    assert.equal(f.spans().length, 1);
    assert.match(JSON.stringify(f.spans()), /PACKED_PROMPT/);
    assert.doesNotMatch(command, /node_modules\/.cache|_npx|emit.py/);
    const broken = await run('/bin/sh', ['-c', command], { ...options, input: 'not json' });
    assert.deepEqual(broken, { code: 0, stdout: '', stderr: '' });
  });
}
test('missing runtime node fails neutral and never invokes network downloader', async (t) => {
  const f = await fixture(t); await augmentAdapter.install(f.context);
  const runtimeRoot = path.join(f.config.paths.configDir, 'runtime/augment/0.1.0');
  const dir = path.join(runtimeRoot, (await fs.readdir(runtimeRoot))[0]);
  await fs.writeFile(path.join(dir, 'bin/node-path'), '/nonexistent/node\n');
  const result = await run('/bin/sh', [path.join(dir, 'bin/futureagi-hook.sh')], { env: { PATH: '' }, input: '{}' });
  assert.deepEqual(result, { code: 0, stdout: '', stderr: '' });
  assert.equal((await augmentAdapter.inspect(f.context)).registered, false);
});
