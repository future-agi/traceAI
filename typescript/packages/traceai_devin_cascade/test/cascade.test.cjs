const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { spawn } = require('node:child_process');
const { setTimeout: delay } = require('node:timers/promises');
const core = require('@traceai/agent-core');
const reader = require('../dist/transcript.js');
const runtime = require('../dist/runtime.js');
const reg = require('../dist/registration.js');
const { cascadeAdapter, qualification } = require('../dist/index.js');
const sample = () => fs.readFile(path.join(__dirname, 'fixtures/documented-transcript.jsonl'));
const attrs = s => Object.fromEntries((s.attributes || []).map(a => [a.key, Object.values(a.value)[0]]));
const spans = f => f.requests.flatMap(r => (r.body.resourceSpans || []).flatMap(rs => rs.scopeSpans.flatMap(s => s.spans)));
async function fixture(t, respond) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'cascade-'));
  const requests = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', b => chunks.push(b));
    req.on('end', () => {
      const body = JSON.parse(Buffer.concat(chunks)); requests.push({ body, headers: req.headers });
      res.setHeader('content-type', 'application/json');
      if (body.resourceSpans.length && respond) respond(res); else res.end('{}');
    });
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const options = { home, env: {} };
  await core.setup({ ...options, agent: runtime.AGENT, configureOnly: true,
    projectName: 'cascade-test', otlpEndpoint: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
    credentials: { apiKey: 'test-api', secretKey: 'test-secret' } });
  const config = await core.resolveConfig({ ...options, agent: runtime.AGENT });
  const transcript = path.join(home, '.windsurf/transcripts/session.jsonl');
  await fs.mkdir(path.dirname(transcript), { recursive: true, mode: 0o700 });
  const context = { config, scope: 'user' };
  const hook = (action, execution = 'turn-1', prompt = 'create a hello world file') => ({
    agent_action_name: action, trajectory_id: 'session', execution_id: execution, timestamp: new Date().toISOString(),
    tool_info: action === 'pre_user_prompt' ? { user_prompt: prompt } : { transcript_path: transcript },
  });
  t.after(async () => { server.closeAllConnections(); await new Promise(r => server.close(r)); await fs.rm(home, { recursive: true, force: true }); });
  return { home, requests, options, config, context, transcript, hook };
}
async function save(f, data) { await fs.writeFile(f.transcript, data, { mode: 0o600 }); }

test('documented example maps one prompt, two reconstructed responses, one tool; no invented model usage', async () => {
  const p = reader.parseSnapshot(await sample(), 'key');
  assert.equal(p.ordinal, 1); assert.equal(p.steps.length, 3); assert.equal(p.outcome, 'success');
  assert.deepEqual(p.steps.map(s => s.kind), ['generation', 'tool', 'generation']);
  assert.equal(p.output, 'I created the file for you.');
  assert.throws(() => reader.parseSnapshot(Buffer.from('{}'), 'key'), /PARTIAL/);
  assert.throws(() => reader.parseSnapshot(Buffer.from('{bad}\n'), 'key'), /MALFORMED/);
});
test('reconstructed generation context stays bounded and never nests previous request inputs', () => {
  const rows = [{ status: 'done', type: 'user_input', user_input: { user_response: 'public prompt' } }];
  for (let i = 0; i < 100; i++) rows.push({ status: 'done', type: 'planner_response', planner_response: { response: `reply-${i}` } });
  const parsed = reader.parseSnapshot(Buffer.from(rows.map(r => JSON.stringify(r)).join('\n') + '\n'), 'key');
  assert.equal(parsed.steps.length, 100);
  for (const step of parsed.steps) {
    assert.ok(step.input.length < 400);
    const input = JSON.parse(step.input);
    assert.equal(input.prompt, 'public prompt');
    assert.ok(input.context.length <= 3);
    assert.ok(input.context.every(previous => previous.input === undefined));
  }
});

test('cursor rejects truncation, rewrites and stale snapshots; second prompt has stable ordinal', async () => {
  const b = await sample(), p = reader.parseSnapshot(b, 'key');
  assert.throws(() => reader.parseSnapshot(b, 'key', p.cursor), /STALE/);
  assert.throws(() => reader.parseSnapshot(b.subarray(0, 20), 'key', p.cursor), /PARTIAL|REWRITTEN/);
  assert.throws(() => reader.parseSnapshot(Buffer.from(b.toString().replace('hello', 'HELLO')), 'key', p.cursor), /REWRITTEN/);
  const second = reader.parseSnapshot(Buffer.concat([b, b]), 'key', p.cursor);
  assert.equal(second.ordinal, 2); assert.equal(second.steps.length, 3);
});
test('unknown schema is incomplete rather than a fabricated tool or generation', async () => {
  const bytes = Buffer.concat([await sample(), Buffer.from('{"type":"new_shape","status":"done","tokens":900}\n')]);
  const parsed = reader.parseSnapshot(bytes, 'key');
  assert.equal(parsed.outcome, 'incomplete'); assert.deepEqual(parsed.gaps, ['UNKNOWN_STEP_SCHEMA']);
  assert.equal(parsed.steps.length, 3);
});
test('source limits and traversal are rejected', async () => {
  assert.throws(() => reader.parseHook({ agent_action_name: 'pre_user_prompt', trajectory_id: '../x', execution_id: 'x', tool_info: {} }), /INVALID/);
  const event = reader.parseHook({ agent_action_name: 'post_cascade_response_with_transcript', trajectory_id: 'x', execution_id: 'x', tool_info: { transcript_path: '/tmp/credentials.json' } });
  assert.throws(() => reader.transcriptPath('/tmp/home', event), /UNTRUSTED/);
  assert.throws(() => reader.parseSnapshot(Buffer.alloc(reader.MAX_TRANSCRIPT_BYTES + 1, 10), 'key'), /LIMIT/);
});
test('asynchronous creation and partial writes settle; pruning is explicit', async t => {
  const f = await fixture(t), b = await sample();
  const reading = reader.settledTranscript(f.transcript, 'key');
  await delay(30); await save(f, b.subarray(0, 100)); await delay(90); await save(f, b);
  assert.equal((await reading).steps.length, 3);
  await fs.unlink(f.transcript);
  await assert.rejects(reader.settledTranscript(f.transcript, 'key'), /MISSING_OR_PRUNED/);
});
test('unsafe mode, symlink, hardlink and FIFO cannot be read as transcripts', async t => {
  const f = await fixture(t); await save(f, await sample());
  await fs.chmod(f.transcript, 0o644); await assert.rejects(reader.readSnapshot(f.transcript), /UNSAFE/);
  await fs.chmod(f.transcript, 0o600);
  const other = f.transcript + '.other'; await fs.link(f.transcript, other); await assert.rejects(reader.readSnapshot(f.transcript), /UNSAFE/);
  await fs.unlink(other); await fs.rename(f.transcript, other); await fs.symlink(other, f.transcript);
  await assert.rejects(reader.readSnapshot(f.transcript));
});
test('two actual packaged adapter turns export eight spans, same session, reconstructed generations, and deduplicate', async t => {
  const f = await fixture(t), b = await sample();
  assert.equal(await runtime.handleHook(f.hook('pre_user_prompt'), f.options), 'PROMPT_OBSERVED');
  await save(f, b);
  assert.equal(await runtime.handleHook(f.hook('post_cascade_response_with_transcript'), f.options), 'EXPORTED');
  assert.equal(await runtime.handleHook(f.hook('post_cascade_response_with_transcript'), f.options), 'DUPLICATE');
  await runtime.handleHook(f.hook('pre_user_prompt', 'turn-2'), f.options);
  await save(f, Buffer.concat([b, b]));
  assert.equal(await runtime.handleHook(f.hook('post_cascade_response_with_transcript', 'turn-2'), f.options), 'EXPORTED');
  const all = spans(f), roots = all.filter(s => !s.parentSpanId);
  assert.equal(all.length, 8); assert.equal(roots.length, 2); assert.notEqual(roots[0].traceId, roots[1].traceId);
  assert.equal(attrs(roots[0])['session.id'], attrs(roots[1])['session.id']);
  const generations = all.filter(s => attrs(s)['fi.span.kind'] === 'LLM');
  assert.equal(generations.length, 4);
  assert.ok(generations.every(s => attrs(s)['fi.agent.fidelity'] === 'reconstructed-message'));
  assert.ok(generations.every(s => attrs(s)['input.value']?.includes('create a hello world file')));
  assert.ok(all.filter(s => attrs(s)['fi.span.kind'] === 'TOOL').every(s => attrs(s)['tool.name'] === 'cascade.code_action'));
  for (const child of all.filter(s => s.parentSpanId)) assert.ok(roots.some(r => r.spanId === child.parentSpanId && r.traceId === child.traceId));
  for (const s of all) assert.ok(!Object.keys(attrs(s)).some(k => /token_count|gen_ai\.cost\.total/.test(k)));
  assert.equal(f.requests.at(-1).headers['x-project-name'], 'cascade-test');
  assert.equal(f.requests.at(-1).body.resourceSpans[0].resource.attributes.find(a => a.key === 'project_type').value.stringValue, 'observe');
  const stateDir = path.join(f.config.paths.stateDir, 'cascade-sessions');
  for (const name of await fs.readdir(stateDir)) {
    const p = path.join(stateDir, name), state = await fs.readFile(p, 'utf8');
    assert.ok(!/hello world|file.py|test-secret|test-api/.test(state)); assert.equal((await fs.stat(p)).mode & 0o777, 0o600);
  }
  const status = await core.inspectStatus({ ...f.options, agent: runtime.AGENT });
  assert.equal(status.activation, 'OBSERVED'); assert.equal(status.delivery.exported, 8);
});
test('missing boundaries and late asynchronous responses cannot attach to a new prompt', async t => {
  const f = await fixture(t); await save(f, await sample());
  await assert.rejects(runtime.handleHook(f.hook('post_cascade_response_with_transcript'), f.options), /BOUNDARY/);
  await runtime.handleHook(f.hook('pre_user_prompt'), f.options);
  await runtime.handleHook(f.hook('pre_user_prompt', 'turn-2', 'different'), f.options);
  await assert.rejects(runtime.handleHook(f.hook('post_cascade_response_with_transcript'), f.options), /BOUNDARY/);
  assert.equal(spans(f).length, 0);
});
test('same prompt cannot consume an unchanged old transcript', async t => {
  const f = await fixture(t); await save(f, await sample());
  await runtime.handleHook(f.hook('pre_user_prompt'), f.options);
  await assert.rejects(runtime.handleHook(f.hook('post_cascade_response_with_transcript'), f.options), /STALE/);
  assert.equal(spans(f).length, 0);
});
test('structure-only privacy, credential redaction and kill switch persist across hook processes', async t => {
  const f = await fixture(t);
  const bytes = Buffer.from((await sample()).toString().replaceAll('hello world', 'test-secret'));
  const prompt = 'create a test-secret file';
  await runtime.handleHook(f.hook('pre_user_prompt', 'turn-1', prompt), f.options); await save(f, bytes);
  await runtime.handleHook(f.hook('post_cascade_response_with_transcript'), f.options);
  assert.ok(!JSON.stringify(spans(f)).includes('test-secret'));
  const options = { ...f.options, env: { FI_AGENT_CAPTURE_CONTENT: 'false' } };
  await runtime.handleHook(f.hook('pre_user_prompt', 'turn-2', prompt), options); await save(f, Buffer.concat([bytes, bytes]));
  await runtime.handleHook(f.hook('post_cascade_response_with_transcript', 'turn-2'), options);
  for (const s of spans(f).slice(4)) assert.ok(!Object.keys(attrs(s)).some(k => k === 'input.value' || k === 'output.value'));
  await core.disable({ ...f.options, agent: runtime.AGENT });
  assert.equal(await runtime.handleHook(f.hook('pre_user_prompt', 'turn-3'), f.options), 'DISABLED');
  assert.equal(spans(f).length, 8);
});
test('export rejection is recorded and not replayed as a new trace', async t => {
  const f = await fixture(t, res => { res.statusCode = 401; res.end('{}'); });
  await runtime.handleHook(f.hook('pre_user_prompt'), f.options); await save(f, await sample());
  assert.equal(await runtime.handleHook(f.hook('post_cascade_response_with_transcript'), f.options), 'EXPORT_DROPPED');
  const count = f.requests.length;
  assert.equal(await runtime.handleHook(f.hook('post_cascade_response_with_transcript'), f.options), 'DUPLICATE');
  assert.equal(f.requests.length, count);
});
test('JSONC registration preserves comments and unrelated hooks; ownership changes are refused', () => {
  const original = '{\n// Keep this\n"other": true, "hooks": {"pre_user_prompt": [{"command":"echo existing"}]}}';
  const command = "'/node' '/bundle' --futureagi-cascade";
  const first = reg.mergeHooks(original, command), second = reg.mergeHooks(first, command);
  assert.equal(second, first); assert.match(second, /Keep this/); assert.match(second, /echo existing/);
  const removed = reg.mergeHooks(second, command, undefined, true); assert.ok(!removed.includes(command)); assert.match(removed, /echo existing/);
  assert.throws(() => reg.mergeHooks(first.replace('/bundle', '/tampered'), command), /MODIFIED/);
  assert.throws(() => reg.mergeHooks(first.replace('"show_output": false', '"show_output": true'), command), /MODIFIED/);
  const powershell = '{"hooks":{"pre_user_prompt":[{"powershell":"echo keep"}]}}';
  assert.match(reg.mergeHooks(powershell, command), /echo keep/);
  assert.match(reg.mergeHooks(reg.mergeHooks(powershell, command), command, undefined, true), /echo keep/);
});
test('safe persistent registration is idempotent, preserves legacy workspace selection, and uninstalls only owned entries', async t => {
  const f = await fixture(t);
  const hookPath = await reg.hookTarget(f.context);
  await fs.writeFile(hookPath, '{\n// keep\n"hooks": {"pre_user_prompt": [{"command":"echo custom"}]}}', { mode: 0o644 });
  const base = path.join(f.home, 'runtime');
  assert.equal((await reg.installHooks(f.context, base)).registered, true);
  const first = await fs.readFile(hookPath, 'utf8');
  assert.equal((await reg.installHooks(f.context, base)).registered, true);
  assert.equal(await fs.readFile(hookPath, 'utf8'), first); assert.ok(!first.includes('_npx')); assert.match(first, /keep/);
  assert.equal((await reg.uninstallHooks(f.context)).registered, false);
  assert.match(await fs.readFile(hookPath, 'utf8'), /echo custom/);
  const project = path.join(f.home, 'project'); await fs.mkdir(path.join(project, '.windsurf'), { recursive: true, mode: 0o700 });
  const legacy = path.join(project, '.windsurf/hooks.json'); await fs.writeFile(legacy, '{"hooks":{"pre_read_code":[{"command":"echo preserved"}]}}', { mode: 0o644 });
  const pc = { ...f.context, scope: 'project', projectDir: project };
  assert.equal(await reg.hookTarget(pc), legacy);
  await fs.mkdir(path.join(project, '.devin'), { mode: 0o700 });
  await fs.writeFile(path.join(project, '.devin/hooks.json'), '{"hooks":{"pre_read_code":[{"command":"echo modern"}]}}', { mode: 0o644 });
  assert.equal(await reg.hookTarget(pc), path.join(project, '.devin/hooks.json'));
});
test('host support fails closed for old, unknown and unqualified versions; package is private', async t => {
  const f = await fixture(t);
  assert.equal(require('../package.json').private, true);
  assert.equal(qualification({ product: 'Windsurf', version: '1.12.0' }).reason, 'CASCADE_TRANSCRIPT_HOOK_UNAVAILABLE');
  assert.equal(qualification({ product: 'Devin', version: '1.9566.9' }).supported, false);
  assert.equal(qualification({ product: 'Devin', version: '3.11.238' }).supported, false);
  await assert.rejects(cascadeAdapter.install(f.context), /CASCADE_/);
});
test('installed standalone hook command exports through real subprocesses with an empty GUI PATH', async t => {
  const f = await fixture(t);
  await reg.installHooks(f.context, path.join(f.home, 'persistent-runtime'));
  const hooks = JSON.parse(await fs.readFile(await reg.hookTarget(f.context), 'utf8')).hooks;
  const run = action => new Promise((resolve, reject) => {
    const child = spawn('/bin/bash', ['-c', hooks[action][0].command], { cwd: f.home,
      env: { HOME: f.home, PATH: '/no-tools' } });
    let stdout = '', stderr = ''; child.stdout.on('data', d => stdout += d); child.stderr.on('data', d => stderr += d);
    child.on('error', reject); child.on('close', code => resolve({ code, stdout, stderr })); child.stdin.end(JSON.stringify(f.hook(action)));
  });
  assert.deepEqual(await run('pre_user_prompt'), { code: 0, stdout: '', stderr: '' });
  await save(f, await sample());
  assert.deepEqual(await run('post_cascade_response_with_transcript'), { code: 0, stdout: '', stderr: '' });
  assert.equal(spans(f).length, 4);
  assert.equal(spans(f).filter(s => !s.parentSpanId).length, 1);
  const status = await core.inspectStatus({ ...f.options, agent: runtime.AGENT, adapter: cascadeAdapter });
  assert.equal(status.registration, 'REGISTERED'); assert.equal(status.activation, 'OBSERVED'); assert.equal(status.delivery.exported, 4);
  await core.disable({ ...f.options, agent: runtime.AGENT });
  assert.deepEqual(await run('post_cascade_response_with_transcript'), { code: 0, stdout: '', stderr: '' });
  assert.equal(spans(f).length, 4);
});
test('bundled hook is silent, fail-open and does not need workspace node_modules', async t => {
  const f = await fixture(t);
  const exe = path.join(__dirname, '../dist/hook.cjs');
  const run = data => new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [exe], { cwd: f.home, env: { HOME: f.home, PATH: '/nonexistent', FI_AGENT_ENABLED: 'false' } });
    let stdout = '', stderr = ''; child.stdout.on('data', d => stdout += d); child.stderr.on('data', d => stderr += d);
    child.on('error', reject); child.on('close', code => resolve({ code, stdout, stderr })); child.stdin.end(data);
  });
  assert.deepEqual(await run('{bad'), { code: 0, stdout: '', stderr: '' });
  assert.deepEqual(await run(JSON.stringify(f.hook('pre_user_prompt'))), { code: 0, stdout: '', stderr: '' });
  assert.deepEqual(await run('x'.repeat(256 * 1024 + 1)), { code: 0, stdout: '', stderr: '' });
});


for (const terminal of ['done','error','interrupted']) test(`recovered action derives root from terminal ${terminal}`, async t=>{
  const f=await fixture(t);
  await runtime.handleHook(f.hook('pre_user_prompt'),f.options);
  const rows=[{type:'user_input',status:'done',user_input:{user_response:'create a hello world file'}},
    {type:'code_action',status:'error',code_action:{path:'first',new_content:'failed'}},
    {type:'code_action',status:'done',code_action:{path:'second',new_content:'replacement'}},
    {type:'planner_response',status:terminal,planner_response:{response:'finished'}}];
  await save(f, rows.map(JSON.stringify).join('\n')+'\n');
  await runtime.handleHook(f.hook('post_cascade_response_with_transcript'),f.options);
  const exported=spans(f), root=exported.find(s=>!s.parentSpanId);
  assert.ok(root,JSON.stringify(exported));
  assert.equal(attrs(root)['fi.agent.outcome'],terminal==='done'?'success':terminal==='interrupted'?'interrupted':'error');
  assert.ok(exported.some(s=>attrs(s)['tool.name']==='cascade.code_action' && s.status.code===2));
});
