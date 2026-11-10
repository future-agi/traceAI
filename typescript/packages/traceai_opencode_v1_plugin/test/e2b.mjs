// Real pinned OpenCode, packed npm artifacts, loopback registry/model/OTLP.
// Execute ONLY in the disposable E2B image; all credentials below are synthetic.
import { createServer, request } from 'node:http';
function npmFetch(path) {
  return new Promise((resolve, reject) => {
    const req = request({ socketPath: '/work/npm-fixture.sock', path: '/npm/' + path }, res => {
      const chunks = []; res.on('data', c => chunks.push(c)); res.on('end', () => {
        const bytes = Buffer.concat(chunks);
        resolve({ ok: res.statusCode === 200, status: res.statusCode, json: async () => JSON.parse(bytes), arrayBuffer: async () => bytes });
      });
    }); req.on('error', reject); req.end();
  });
}
import { readFile, writeFile, mkdir, readdir, chmod, chown, stat } from 'node:fs/promises';
import { spawn, execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import assert from 'node:assert/strict';
const OUT = '/work/evidence'; await mkdir(OUT, { recursive: true });
const USER = 1000, GROUP = 1000, HOME = '/work/isolated';
await mkdir(HOME, { recursive: true }); await chown(HOME, USER, GROUP); await chmod(HOME, 0o700);
const project = join(HOME, 'project'); await mkdir(project); await chown(project, USER, GROUP);
await writeFile(join(project, 'README.md'), '# Synthetic sample\nA tiny project used for an OpenCode trace.\n');
const tarballs = new Map();
for (const file of await readdir('/work/packs')) {
  const path = join('/work/packs', file);
  const metadata = JSON.parse(execFileSync('tar', ['-xOf', path, 'package/package.json'], { encoding: 'utf8' }));
  tarballs.set(metadata.name, { metadata, file, bytes: await readFile(path) });
}
const requests = [], captures = [], registryRequests = [], errors = [];
let round = 0, rejectExports = false, rejectedExports = 0;
const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://127.0.0.1');
    if (url.pathname.startsWith('/registry/')) {
      const path = url.pathname.slice('/registry/'.length);
      registryRequests.push(path);
      const packed = tarballs.get(decodeURIComponent(path));
      if (packed) {
        const metadata = { ...packed.metadata, dist: { tarball: `http://127.0.0.1:${port}/tarballs/${packed.file}`, integrity: `sha512-${createHash('sha512').update(packed.bytes).digest('base64')}` } };
        res.setHeader('content-type', 'application/json'); res.end(JSON.stringify({ name: metadata.name, 'dist-tags': { latest: metadata.version }, versions: { [metadata.version]: metadata } })); return;
      }
      // Only dependency preparation uses this fixed registry proxy; the agent has
      // no outbound internet. Dependency bodies/metadata are not model requests.
      const response = await npmFetch(path);
      if (!response.ok) { res.writeHead(response.status); res.end(); return; }
      const body = await response.json();
      for (const v of Object.values(body.versions ?? {})) if (v.dist?.tarball?.startsWith('https://registry.npmjs.org/')) v.dist.tarball = v.dist.tarball.replace('https://registry.npmjs.org/', `http://127.0.0.1:${port}/registry-tarball/`);
      res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(body)); return;
    }
    if (url.pathname.startsWith('/registry-tarball/')) {
      const response = await npmFetch(url.pathname.slice('/registry-tarball/'.length));
      res.writeHead(response.status); res.end(Buffer.from(await response.arrayBuffer())); return;
    }
    if (url.pathname.startsWith('/tarballs/')) {
      const packed = [...tarballs.values()].find(p => p.file === url.pathname.split('/').at(-1));
      if (!packed) { res.writeHead(404); res.end(); return; } res.end(packed.bytes); return;
    }
    let raw = ''; for await (const chunk of req) raw += chunk;
    if (url.pathname === '/tracer/v1/traces') {
      assert.equal(req.headers['x-api-key'], 'dummy-api'); assert.equal(req.headers['x-secret-key'], 'dummy-secret'); assert.equal(req.headers['x-project-name'], 'coding-agents');
      if (rejectExports) { rejectedExports++; res.writeHead(503, { 'content-type': 'application/json' }); res.end('{}'); return; }
      captures.push(JSON.parse(raw)); res.setHeader('content-type', 'application/json'); res.end('{}'); return;
    }
    if (url.pathname === '/v1/chat/completions') {
      const data = JSON.parse(raw); requests.push(data);
      const lastUser = data.messages.findLastIndex(m => m.role === 'user');
      const toolUsed = data.messages.slice(lastUser + 1).some(m => m.role === 'tool');
      const isAgent = Array.isArray(data.tools) && data.tools.length > 0;
      const failTool = JSON.stringify(data.messages.slice(lastUser)).includes('FAIL_TOOL');
      const userText = JSON.stringify(data.messages[lastUser]);
      if (isAgent && (userText.includes('TERMINAL_AUTH_ERROR') || userText.includes('TERMINAL_CONTEXT_ERROR'))) {
        const context = userText.includes('TERMINAL_CONTEXT_ERROR');
        res.writeHead(context ? 400 : 401, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: { message: context ? 'maximum context length exceeded' : 'Invalid API key', type: context ? 'invalid_request_error' : 'authentication_error', code: context ? 'context_length_exceeded' : 'invalid_api_key' } })); return;
      }
      if (isAgent && userText.includes('INTERRUPT_STREAM')) {
        res.writeHead(200, { 'content-type': 'text/event-stream' });
        res.write(`data: ${JSON.stringify({ id: 'chatcmpl-interrupted', object: 'chat.completion.chunk', model: 'test', choices: [{ index: 0, delta: { role: 'assistant', content: 'Partial answer before cancellation' }, finish_reason: null }] })}\n\n`);
        // Deliberately unfinished: the real host's abort endpoint cancels it.
        return;
      }
      const reuse = userText.includes('REUSE_TASK'), background = userText.includes('BACKGROUND_PARENT');
      const childPrompt = userText.includes('CHILD_ONE') || userText.includes('CHILD_TWO');
      const notification = userText.includes('<task id=');
      const replies = data.messages.slice(lastUser + 1).filter(m => m.role === 'tool');
      const n = replies.length;
      const tool = isAgent && (reuse ? n < 2 : background ? n < 1 : childPrompt || notification ? false : !toolUsed);
      const taskArgs = { description: `Review child ${n + 1}`, prompt: n === 0 ? 'CHILD_ONE' : 'CHILD_TWO', subagent_type: 'general',
        ...(background ? { background: true } : {}), ...(reuse && n === 1 ? { task_id: JSON.stringify(replies).match(/ses_[A-Za-z0-9]+/)?.[0] } : {}) };
      const call = { index: 0, id: `call_mock_${++round}`, type: 'function', function: { name: reuse || background ? 'task' : 'read', arguments: JSON.stringify(reuse || background ? taskArgs : { filePath: join(project, failTool ? 'missing.txt' : 'README.md') }) } };
      const content = !isAgent ? 'Synthetic project' : reuse || background ? 'PARENT_COMPLETE' : notification ? 'BACKGROUND_ACK' : childPrompt ? (userText.includes('CHILD_TWO') ? 'CHILD_TWO_COMPLETE' : 'CHILD_ONE_COMPLETE') : failTool ? 'The file is missing.' : 'This project contains a synthetic sample.';
      if (isAgent && childPrompt && !notification) await new Promise(resolve => setTimeout(resolve, 2500));
      const usage = { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120, prompt_tokens_details: { cached_tokens: 10 }, completion_tokens_details: { reasoning_tokens: 5 } };
      const message = { role: 'assistant', content: tool ? null : content, ...(tool ? { tool_calls: [call] } : {}) };
      if (!data.stream) { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify({ id: `chatcmpl-${round}`, object: 'chat.completion', model: 'test', choices: [{ index: 0, message, finish_reason: tool ? 'tool_calls' : 'stop' }], usage })); return; }
      res.setHeader('content-type', 'text/event-stream');
      const send = x => res.write(`data: ${JSON.stringify({ id: `chatcmpl-${round}`, object: 'chat.completion.chunk', created: Math.floor(Date.now()/1000), model: 'test', ...x })}\n\n`);
      send({ choices: [{ index: 0, delta: { role: 'assistant' }, finish_reason: null }] });
      send({ choices: [{ index: 0, delta: tool ? { tool_calls: [call] } : { content }, finish_reason: null }] });
      send({ choices: [{ index: 0, delta: {}, finish_reason: tool ? 'tool_calls' : 'stop' }], usage });
      send({ choices: [], usage }); res.end('data: [DONE]\n\n'); return;
    }
    errors.push({ path: url.pathname }); res.writeHead(404); res.end('{}');
  } catch (error) { errors.push({ error: String(error) }); res.writeHead(500); res.end('{}'); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const port = server.address().port;
const env = {
  PATH: `/work/host/node_modules/.bin:${process.env.PATH}`, HOME,
  XDG_CONFIG_HOME: join(HOME, '.config'), XDG_DATA_HOME: join(HOME, '.local/share'), XDG_STATE_HOME: join(HOME, '.local/state'), XDG_CACHE_HOME: join(HOME, '.cache'),
  FI_API_KEY: 'dummy-api', FI_SECRET_KEY: 'dummy-secret', FI_OTLP_ENDPOINT: `http://127.0.0.1:${port}/tracer/v1/traces`,
  npm_config_registry: `http://127.0.0.1:${port}/registry/`,
  OPENCODE_DISABLE_DEFAULT_PLUGINS: 'true', OPENCODE_DISABLE_MODELS_FETCH: 'true', OPENCODE_DISABLE_AUTOUPDATE: 'true', OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER: 'true',
  DO_NOT_TRACK: '1', NO_COLOR: '1', CI: '1',
};
// isolated-e2b.mjs creates a network namespace with ONLY the loopback link.
assert.deepEqual(JSON.parse(execFileSync('ip', ['-j', 'route'], { encoding: 'utf8' })), []);
async function run(name, command, args, extra = {}, expected = 0) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd: project, env: { ...env, ...extra }, uid: USER, gid: GROUP, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    child.stdout.on('data', x => stdout += x); child.stderr.on('data', x => stderr += x);
    const timer = setTimeout(() => child.kill('SIGKILL'), 150000);
    child.on('error', reject);
    child.on('close', async code => {
      clearTimeout(timer); await writeFile(join(OUT, `${name}.log`), `exit=${code}\n${stdout}\nSTDERR\n${stderr}`);
      if (expected !== null && code !== expected) reject(new Error(`${name}: exit ${code}; ${stderr.slice(-500)}`)); else resolve({ code, stdout, stderr });
    });
  });
}
const cli = '/work/typescript/packages/traceai_agents/dist/cli.js';
try {
  await run('version', 'opencode', ['--version']);
  await run('setup', process.execPath, [cli, 'setup', 'opencode-v1', '--project', 'coding-agents', '--json']);
  await run('setup-repeat', process.execPath, [cli, 'setup', 'opencode-v1', '--project', 'coding-agents', '--json']);
  assert.equal((await stat(join(HOME, '.config/futureagi/agents/credentials.json'))).mode & 0o777, 0o600);
  assert.equal((await stat(join(HOME, '.config/futureagi/agents'))).mode & 0o777, 0o700);
  delete env.FI_API_KEY; delete env.FI_SECRET_KEY; delete env.FI_OTLP_ENDPOINT;
  const cfgPath = join(HOME, '.config/opencode/opencode.json');
  const cfg = JSON.parse(await readFile(cfgPath, 'utf8'));
  assert.deepEqual(cfg.plugin, ['@traceai/opencode-v1-plugin@0.1.0']);
  cfg.model = 'mock/test'; cfg.small_model = 'mock/test'; cfg.enabled_providers = ['mock']; cfg.share = 'disabled'; cfg.autoupdate = false;
  cfg.provider = { mock: { npm: '@ai-sdk/openai-compatible', name: 'Mock', options: { baseURL: `http://127.0.0.1:${port}/v1`, apiKey: 'dummy-model' }, models: { test: { name: 'Test', limit: { context: 128000, output: 4096 }, cost: { input: 1, output: 2 } } } } };
  await writeFile(cfgPath, JSON.stringify(cfg, null, 2)); await chown(cfgPath, USER, GROUP); await chmod(cfgPath, 0o600);
  await run('prompt-one', 'opencode', ['run', '--model', 'mock/test', '--format', 'json', 'Read README.md and summarize it.']);
  await run('prompt-two', 'opencode', ['run', '--continue', '--model', 'mock/test', '--format', 'json', 'FAIL_TOOL Read missing.txt and report the error.']);
  await run('prompt-new-session', 'opencode', ['run', '--model', 'mock/test', '--format', 'json', 'Read README.md once more.']);
  const beforeDisable = captures.flatMap(b => b.resourceSpans ?? []).flatMap(r => r.scopeSpans ?? []).flatMap(s => s.spans ?? []);
  await run('status', process.execPath, [cli, 'status', 'opencode-v1', '--json', '--check']);
  await run('disable', process.execPath, [cli, 'disable', 'opencode-v1', '--json']);
  await run('prompt-disabled', 'opencode', ['run', '--model', 'mock/test', '--format', 'json', 'Read README.md with tracing disabled.']);
  const spans = captures.flatMap(b => b.resourceSpans ?? []).flatMap(r => r.scopeSpans ?? []).flatMap(s => s.spans ?? []);
  assert.equal(spans.length, beforeDisable.length);
  const attrs = span => Object.fromEntries(span.attributes.map(a => [a.key, a.value.stringValue ?? a.value.intValue ?? a.value.doubleValue ?? a.value.boolValue]));
  const roots = spans.filter(s => !s.parentSpanId), llms = spans.filter(s => attrs(s)['fi.span.kind'] === 'LLM'), tools = spans.filter(s => attrs(s)['fi.span.kind'] === 'TOOL');
  assert.equal(roots.length, 3); assert.equal(new Set(roots.map(s => s.traceId)).size, 3);
  assert.equal(new Set(roots.map(s => attrs(s)['session.id'])).size, 2);
  assert.equal(llms.length, 6); assert.equal(tools.length, 3);
  assert.equal(tools.filter(s => s.status.code === 2).length, 1);
  assert.equal(new Set(spans.map(s => s.spanId)).size, spans.length);
  for (const s of llms) { const a = attrs(s); assert.equal(Number(a['gen_ai.usage.input_tokens']), 100); assert.equal(Number(a['gen_ai.usage.output_tokens']), 20); assert.equal(a['fi.agent.cost.source'], 'host-estimate'); assert.ok(a['input.value']); assert.ok(a['output.value'], 'tool-call-only generations have output'); assert.ok(Number(a['gen_ai.cost.total']) > 0); assert.equal(a['gen_ai.usage.cost'], undefined); }
  for (const tool of tools) { assert.ok(llms.some(llm => llm.spanId === tool.parentSpanId && llm.traceId === tool.traceId)); assert.equal(attrs(tool)['tool.name'], tool.name); assert.match(attrs(tool)['tool_call.id'], /^call_mock_\d+$/); }
  assert.ok(registryRequests.some(p => decodeURIComponent(p) === '@traceai/opencode-v1-plugin'));
  assert.ok(!JSON.stringify(spans).includes('dummy-secret'));
  assert.equal(errors.length, 0);
  await run('enable', process.execPath, [cli, 'enable', 'opencode-v1', '--json']);
  await run('prompt-private', 'opencode', ['run', '--model', 'mock/test', '--format', 'json', 'Read README.md privately.'], { FI_AGENT_CAPTURE_CONTENT: 'false' });
  const all = () => captures.flatMap(b => b.resourceSpans ?? []).flatMap(r => r.scopeSpans ?? []).flatMap(s => s.spans ?? []);
  const privateSpans = all().slice(spans.length);
  assert.equal(privateSpans.length, 4);
  assert.ok(privateSpans.every(s => !('input.value' in attrs(s)) && !('output.value' in attrs(s))));
  // Real-host regressions: resumed child sessions and background completion
  // after the original parent goes idle. No provider or Cloud egress exists.
  const reuseStart = all().length;
  await run('reuse-task', 'opencode', ['run', '--model', 'mock/test', '--format', 'json', 'REUSE_TASK']);
  const reuseSpans = all().slice(reuseStart);
  assert.equal(reuseSpans.filter(s => !s.parentSpanId).length, 1);
  assert.equal(reuseSpans.filter(s => attrs(s)['fi.span.kind'] === 'LLM').length, 5);
  const branches = ['CHILD_ONE', 'CHILD_TWO'].map(marker => reuseSpans.find(s => s.name === 'subagent' && attrs(s)['input.value'] === marker));
  assert.ok(branches.every(Boolean));
  assert.notEqual(branches[0].parentSpanId, branches[1].parentSpanId);
  const firstTask = reuseSpans.find(s => s.spanId === branches[0].parentSpanId);
  const secondTask = reuseSpans.find(s => s.spanId === branches[1].parentSpanId);
  const childSession = attrs(firstTask)['output.value'].match(/ses_[A-Za-z0-9]+/)?.[0];
  assert.ok(childSession);
  assert.equal(JSON.parse(attrs(secondTask)['input.value']).task_id, childSession, 'second task resumes exactly the first child session');
  for (const [i, branch] of branches.entries()) {
    const parent = reuseSpans.find(s => s.spanId === branch.parentSpanId);
    assert.equal(parent.name, 'task');
    assert.ok(attrs(parent)['input.value'].includes(i ? 'CHILD_TWO' : 'CHILD_ONE'));
    assert.equal(attrs(branch)['fi.agent.outcome'], 'success');
  }
  const backgroundStart = all().length, requestStart = requests.length;
  let backgroundEnd, backgroundRequestEnd;
  const terminalCases = [];
  const host = spawn('opencode', ['serve', '--hostname', '127.0.0.1', '--port', '4096'], { cwd: project, env: { ...env, OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS: 'true' }, uid: USER, gid: GROUP, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
  let hostLog = ''; host.stdout.on('data', x => hostLog += x); host.stderr.on('data', x => hostLog += x);
  try {
    let healthy = false;
    for (let i = 0; i < 150; i++) {
      try { healthy = (await fetch('http://127.0.0.1:4096/global/health')).ok; } catch {}
      if (healthy) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.ok(healthy);
    const response = await fetch('http://127.0.0.1:4096/session', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}', signal: AbortSignal.timeout(10000) });
    assert.ok(response.ok); const session = await response.json();
    const message = await fetch(`http://127.0.0.1:4096/session/${session.id}/message`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ model: { providerID: 'mock', modelID: 'test' }, parts: [{ type: 'text', text: 'BACKGROUND_PARENT' }] }), signal: AbortSignal.timeout(20000) });
    assert.ok(message.ok); await writeFile(join(OUT, 'background-parent.json'), await message.text());
    for (let i = 0; i < 150 && !all().slice(backgroundStart).some(s => !s.parentSpanId); i++) await new Promise(resolve => setTimeout(resolve, 100));
    backgroundEnd = all().length; backgroundRequestEnd = requests.length;
    const interruptedStart = all().length;
    const abortSession = await (await fetch('http://127.0.0.1:4096/session', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' })).json();
    const streaming = fetch(`http://127.0.0.1:4096/session/${abortSession.id}/message`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ model: { providerID: 'mock', modelID: 'test' }, parts: [{ type: 'text', text: 'INTERRUPT_STREAM' }] }), signal: AbortSignal.timeout(20000) });
    for (let i = 0; i < 100 && !requests.slice(backgroundRequestEnd).some(r => r.tools?.length && JSON.stringify(r.messages).includes('INTERRUPT_STREAM')); i++) await new Promise(r => setTimeout(r, 50));
    assert.ok(requests.slice(backgroundRequestEnd).some(r => r.tools?.length && JSON.stringify(r.messages).includes('INTERRUPT_STREAM')));
    await new Promise(r => setTimeout(r, 100));
    const aborted = await fetch(`http://127.0.0.1:4096/session/${abortSession.id}/abort`, { method: 'POST', signal: AbortSignal.timeout(10000) });
    assert.ok(aborted.ok); await (await streaming).text();
    for (let i = 0; i < 100 && !all().slice(interruptedStart).some(s => !s.parentSpanId); i++) await new Promise(r => setTimeout(r, 50));
    const interrupted = all().slice(interruptedStart);
    const root = interrupted.find(s => !s.parentSpanId);
    assert.equal(attrs(root)['fi.agent.outcome'], 'interrupted');
    const activity = interrupted.find(s => attrs(s)['fi.span.kind'] === 'LLM');
    assert.ok(activity, 'interrupted streaming preserves its model activity');
    assert.equal(attrs(activity)['fi.agent.outcome'], 'interrupted');
    terminalCases.push({ marker: 'INTERRUPT_STREAM', outcome: 'interrupted', spans: interrupted.length });
  } finally {
    try { process.kill(-host.pid, 'SIGTERM'); } catch {}
    host.stdout.destroy(); host.stderr.destroy();
    await Promise.race([new Promise(resolve => host.on('exit', resolve)), new Promise(resolve => setTimeout(resolve, 2000))]);
    await writeFile(join(OUT, 'background-host.log'), hostLog);
  }
  const backgroundSpans = all().slice(backgroundStart, backgroundEnd);
  assert.equal(backgroundSpans.filter(s => !s.parentSpanId).length, 1, 'one submitted prompt, not a synthetic second root');
  const childGeneration = backgroundSpans.find(s => attrs(s)['fi.span.kind'] === 'LLM' && attrs(s)['output.value'] === 'CHILD_ONE_COMPLETE');
  assert.ok(childGeneration, 'delayed child generation is exported');
  assert.equal(Number(attrs(childGeneration)['gen_ai.usage.output_tokens']), 20);
  assert.equal(attrs(childGeneration)['fi.agent.outcome'], 'success');
  const childBranch = backgroundSpans.find(s => s.spanId === childGeneration.parentSpanId);
  assert.equal(childBranch.name, 'subagent');
  assert.equal(backgroundSpans.find(s => s.spanId === childBranch.parentSpanId).name, 'task');
  assert.equal(backgroundSpans.filter(s => attrs(s)['fi.span.kind'] === 'LLM').length, 4);
  assert.equal(requests.slice(requestStart, backgroundRequestEnd).filter(r => r.tools?.length).length, 4);
  assert.ok(backgroundSpans.every(s => attrs(s)['fi.agent.outcome'] === 'success'));
  cfg.compaction = { auto: false };
  await writeFile(cfgPath, JSON.stringify(cfg, null, 2)); await chown(cfgPath, USER, GROUP); await chmod(cfgPath, 0o600);
  for (const marker of ['TERMINAL_AUTH_ERROR', 'TERMINAL_CONTEXT_ERROR']) {
    const start = all().length;
    const result = await run(marker, 'opencode', ['run', '--model', 'mock/test', '--format', 'json', marker], {}, 1);
    const captured = all().slice(start), root = captured.find(s => !s.parentSpanId);
    assert.equal(captured.filter(s => !s.parentSpanId).length, 1);
    assert.equal(root.status.code, 2); assert.equal(attrs(root)['fi.agent.outcome'], 'error');
    const failed = captured.find(s => attrs(s)['fi.span.kind'] === 'LLM');
    assert.ok(failed, `${marker} preserves a source-identified failed generation`);
    assert.equal(failed.status.code, 2); assert.equal(attrs(failed)['fi.agent.outcome'], 'error');
    assert.ok(attrs(failed)['input.value']);
    terminalCases.push({ marker, exit: result.code, outcome: 'error', spans: captured.length });
  }
  await writeFile(join(OUT, 'terminal-cases.json'), JSON.stringify(terminalCases, null, 2));
  const beforeOutage = all().length;
  rejectExports = true;
  await run('prompt-outage', 'opencode', ['run', '--model', 'mock/test', '--format', 'json', 'Read README.md during the telemetry outage.']);
  assert.ok(rejectedExports > 0); assert.equal(all().length, beforeOutage);
  const evidence = { terminalCases, hostVersion: '1.19.0', roots: roots.length, llms: llms.length, tools: tools.length, toolErrors: 1, sessions: 2, spans: spans.length, privateSpans: privateSpans.length, resumedTaskParents: 'distinct', reusedChildLlms: 5, backgroundRoots: 1, backgroundLlms: 4, backgroundChildUsage: 20, outageRejectedExports: rejectedExports, modelRequests: requests.length, registryRequests: registryRequests.length, installedByHost: true, repeatedSetup: 'one plugin', resumedConversation: true, newConversation: true, disabledRunExported: 0, egress: 'unshare network namespace: loopback only; fixed npm dependency broker over Unix socket' };
  await writeFile(join(OUT, 'result.json'), JSON.stringify(evidence, null, 2)); console.log(JSON.stringify(evidence));
} finally {
  await writeFile(join(OUT, 'otlp.json'), JSON.stringify(captures, null, 2));
  await writeFile(join(OUT, 'model-requests.json'), JSON.stringify(requests, null, 2));
  await writeFile(join(OUT, 'registry-requests.json'), JSON.stringify(registryRequests, null, 2));
  await writeFile(join(OUT, 'server-errors.json'), JSON.stringify(errors, null, 2));
  server.close(); server.closeAllConnections();
}
