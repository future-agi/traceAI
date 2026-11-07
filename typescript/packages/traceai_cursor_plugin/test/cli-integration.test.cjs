const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { Readable, Writable } = require('node:stream');
const c = require('@traceai/agent-core');
const { createCliStreamObserver } = require('../dist/cli/stream.js');
const { runCursorCliStream, assertExclusiveCliStream, validatePrintArguments } = require('../dist/cli/run.js');
const { discoverCursorCli } = require('../dist/cli/host.js');
const { cursorCliAdapter } = require('../dist/cli/adapter.js');
async function fixture(t, extra = {}) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'cursor-cli-'));
  const requests = [];
  const server = http.createServer((req, res) => {
    let body = ''; req.on('data', d => body += d); req.on('end', () => {
      requests.push({ headers: req.headers, body: JSON.parse(body) });
      res.setHeader('content-type', 'application/json'); res.end('{}');
    });
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(async () => { server.closeAllConnections(); await new Promise(r => server.close(r)); await fs.rm(home, { recursive: true, force: true }); });
  const options = { home, agent: 'cursor-cli', env: {} };
  const result = await c.setup({ ...options, projectName: 'cursor-fixture', configureOnly: true,
    credentials: { apiKey: 'dummy-api-key-canary', secretKey: 'dummy-secret-key-canary' },
    otlpEndpoint: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`, ...extra });
  assert.equal(result.configured, true);
  const config = await c.resolveConfig(options);
  return { home, requests, config, options, context: { config, scope: 'user', projectDir: home } };
}
const flatten = requests => requests.flatMap(r => r.body.resourceSpans ?? []).flatMap(r => r.scopeSpans ?? []).flatMap(r => r.spans ?? []);
const attrs = span => Object.fromEntries(span.attributes.map(a => [a.key, a.value.stringValue ?? a.value.boolValue ?? a.value.intValue]));
function replay(observer) {
  const events = [
    { type: 'system', subtype: 'init', cwd: '/private/workspace', user_email: 'private@example.invalid' },
    { type: 'tool_call', subtype: 'started', call_id: 'tool-a', tool_call: { readToolCall: { args: { path: 'synthetic.txt', text: 'input dummy-secret-key-canary' } } } },
    { type: 'tool_call', subtype: 'completed', call_id: 'tool-a', tool_call: { readToolCall: { result: { success: { content: 'synthetic tool output' } } } } },
    { type: 'result', subtype: 'success', is_error: false, result: 'terminal dummy-api-key-canary', usage: { input_tokens: 999 } },
  ];
  for (const event of events) observer.push(Buffer.from(JSON.stringify({ session_id: 'synthetic-conversation', ...event }) + '\n'));
  observer.finish(0);
}
test('fixture stream reaches real local OTLP as one aggregate-run + one tool with redaction and no LLM rows', async t => {
  const f = await fixture(t); const tracer = c.createTurnTracer({ config: f.config });
  replay(createCliStreamObserver({ tracer, runId: 'run-one', ownership: 'exclusive-stream' }));
  await tracer.shutdown();
  const spans = flatten(f.requests); assert.equal(spans.length, 2);
  const root = spans.find(s => !s.parentSpanId), child = spans.find(s => s.parentSpanId);
  assert.equal(root.name, 'cursor.run'); assert.equal(attrs(root)['fi.agent.fidelity'], 'aggregate-run');
  assert.equal(attrs(root)['fi.agent.unit'], 'run'); assert.equal(child.parentSpanId, root.spanId);
  assert.equal(child.traceId, root.traceId); assert.equal(attrs(child)['fi.span.kind'], 'TOOL');
  const exported = JSON.stringify(spans);
  for (const privateValue of ['dummy-api-key-canary', 'dummy-secret-key-canary', '/private/workspace', 'private@example.invalid', 'input_tokens']) assert.ok(!exported.includes(privateValue), privateValue);
  assert.ok(exported.includes('[REDACTED]')); assert.ok(!exported.includes('llm.token_count'));
  assert.ok(!spans.some(s => attrs(s)['fi.span.kind'] === 'LLM'));
  for (const req of f.requests) assert.equal(req.headers['x-project-name'], 'cursor-fixture');
  console.log(JSON.stringify({ source: 'synthetic-stream-fixture-not-real-Cursor',
    received: spans.map(span => ({ name: span.name, kind: attrs(span)['fi.span.kind'],
      fidelity: attrs(span)['fi.agent.fidelity'], hasParent: !!span.parentSpanId })) }));
});
test('separate terminal runs share a conversation but never alias a user-turn root', async t => {
  const f = await fixture(t); const tracer = c.createTurnTracer({ config: f.config });
  for (const runId of ['one', 'two']) replay(createCliStreamObserver({ tracer, runId, ownership: 'exclusive-stream' }));
  tracer.startTurn({ sessionId: 'synthetic-conversation', turnId: 'cli-print-run:one' }).end();
  await tracer.shutdown();
  const roots = flatten(f.requests).filter(span => !span.parentSpanId);
  assert.equal(roots.length, 3);
  assert.equal(new Set(roots.map(span => span.traceId)).size, 3);
  assert.equal(new Set(roots.map(span => attrs(span)['session.id'])).size, 1);
  assert.equal(roots.filter(span => attrs(span)['fi.agent.fidelity'] === 'aggregate-run').length, 2);
});
test('metadata-only and persistent disable prevent content and queued delivery', async t => {
  const f = await fixture(t, { captureContent: false });
  const tracer = c.createTurnTracer({ config: f.config });
  replay(createCliStreamObserver({ tracer, runId: 'structure-run', ownership: 'exclusive-stream' }));
  await tracer.shutdown();
  for (const span of flatten(f.requests)) {
    assert.equal(attrs(span)['input.value'], undefined); assert.equal(attrs(span)['output.value'], undefined);
  }
  const before = flatten(f.requests).length;
  const stale = c.createTurnTracer({ config: f.config });
  replay(createCliStreamObserver({ tracer: stale, runId: 'disabled-run', ownership: 'exclusive-stream' }));
  await c.disable(f.options); await stale.shutdown();
  assert.equal(flatten(f.requests).length, before);
});
test('shared hook and preview/plugin owners block the separately held stream route', async t => {
  const f = await fixture(t);
  await assertExclusiveCliStream(f.context);
  const hookPath = path.join(f.home, '.cursor/hooks.json');
  await fs.writeFile(hookPath, '{"version":1,"hooks":{"stop":[{"command":"fi-agent hook cursor"}]}}', { mode: 0o600 });
  await assert.rejects(assertExclusiveCliStream(f.context), /ALIAS_UNPROVEN/);
  await fs.unlink(hookPath);
  await fs.mkdir(path.join(f.home, '.cursor/plugins'));
  await assert.rejects(assertExclusiveCliStream(f.context), /OWNERSHIP_UNVERIFIED/);
  await c.updateJson(f.config.paths.manifest, old => ({ ...old, registrations: { cursor: { version: '0.1.0', scope: 'user', status: 'REGISTERED' } } }));
  await assert.rejects(assertExclusiveCliStream(f.context), /ALIAS_UNPROVEN/);
  await assert.rejects(cursorCliAdapter.install(f.context), /CURSOR_CLI_NOT_FOUND/);
  assert.equal(await cursorCliAdapter.run(f.context, []), 2);
});
test('argument validation does not alter output, permission, trust or plugin behavior', () => {
  validatePrintArguments(['-p', '--output-format', 'stream-json', 'synthetic']);
  assert.throws(() => validatePrintArguments(['--output-format', 'stream-json']), /EXPLICIT_PRINT/);
  assert.throws(() => validatePrintArguments(['-p', '--output-format', 'text']), /STREAM_JSON/);
  assert.throws(() => validatePrintArguments(['-p', '--output-format=stream-json', '--plugin-dir=/other']), /OWNERSHIP/);
});
test('launcher fixtures preserve stdout/stderr and exit code and strip Future AGI keys from child environment', async t => {
  const f = await fixture(t);
  const executable = path.join(f.home, 'synthetic-cursor');
  // This is explicitly a launcher fixture, not evidence of a real Cursor run.
  await fs.writeFile(executable, `#!${process.execPath}\nif (process.env.FI_API_KEY || process.env.FI_SECRET_KEY) process.exit(99);\nprocess.stdout.write(JSON.stringify({type:'result',subtype:'success',session_id:'fixture',result:'synthetic'})+'\\n');\nprocess.stderr.write('host stderr');\nprocess.exitCode=7;\n`, { mode: 0o700 });
  let out = '', err = '';
  const result = await runCursorCliStream({ context: f.context, executable,
    args: ['-p', '--output-format', 'stream-json'], stdin: Readable.from([]),
    env: { ...process.env, FI_API_KEY: 'must-not-inherit', FI_SECRET_KEY: 'must-not-inherit' },
    stdout: new Writable({ write(b, _, done) { out += b; done(); } }),
    stderr: new Writable({ write(b, _, done) { err += b; done(); } }) });
  assert.equal(result.code, 7); assert.equal(err, 'host stderr'); assert.equal(JSON.parse(out).result, 'synthetic');
  const root = flatten(f.requests)[0]; assert.equal(attrs(root)['fi.agent.outcome'], 'error');
});
test('discovery identifies actual CLI help shape without trusting arbitrary version output', async t => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'cli-discovery-'));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const executable = path.join(home, 'agent');
  await fs.writeFile(executable, `#!${process.execPath}\nconsole.log(process.argv.includes('--version') ? '2026.11.05-86047df' : 'Start the Cursor Agent --output-format');`, { mode: 0o700 });
  const found = await discoverCursorCli({ env: { PATH: home }, executable });
  assert.equal(found.installed, true); assert.equal(found.inspectedVersion, true); assert.equal(found.qualified, false);
  await fs.writeFile(executable, `#!${process.execPath}\nconsole.log('2026.11.05-86047df');`, { mode: 0o700 });
  assert.equal((await discoverCursorCli({ env: { PATH: home } })).installed, false);
});
