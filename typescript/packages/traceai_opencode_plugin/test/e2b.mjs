// Run only in the secret-free E2B environment. All model/OTLP traffic is loopback.
import { createServer } from 'node:http';
import { spawn, execFileSync } from 'node:child_process';
import { mkdir, writeFile, readFile, readdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
import assert from 'node:assert/strict';

const root = '/work';
const evidence = join(root, 'evidence');
await mkdir(evidence, { recursive: true });
const receipts = [];
const captures = [];
const models = [];
const home = join(root, 'isolated-home');
const project = join(root, 'sample');
await mkdir(join(home, '.config/opencode'), { recursive: true });
await mkdir(project, { recursive: true });
await writeFile(join(project, 'sample.txt'), 'Public sample file.\n');
const env = { ...process.env, HOME: home, XDG_CONFIG_HOME: join(home, '.config'), XDG_DATA_HOME: join(home, '.local/share'),
  XDG_STATE_HOME: join(home, '.local/state'), XDG_CACHE_HOME: join(home, '.cache'), MOCK_API_KEY: 'mock-only', ...(process.env.FI_TEST_OUTPUT_PRIVACY === '1' ? { FI_HIDE_OUTPUTS: 'true' } : {}),
  FI_API_KEY: 'test-api-key', FI_SECRET_KEY: 'test-secret-key', FI_PROJECT_NAME: 'coding-agents',
  OPENCODE_DISABLE_AUTOUPDATE: '1', OPENCODE_DISABLE_SHARE: '1' };
for (const k of ['ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY']) delete env[k];
const binary = '/work/host/node_modules/.bin/opencode';
async function run(bin, args, options = {}) {
  const p = spawn(bin, args, { cwd: project, env, stdio: ['ignore', 'pipe', 'pipe'], ...options });
  let stdout = '', stderr = '';
  p.stdout.on('data', b => { stdout += b; }); p.stderr.on('data', b => { stderr += b; });
  const timer = setTimeout(() => p.kill('SIGKILL'), 90_000);
  const code = await new Promise((r, j) => { p.on('error', j); p.on('close', r); });
  clearTimeout(timer);
  receipts.push({ bin, args, code, stdout, stderr });
  await writeFile(join(evidence, 'commands.json'), JSON.stringify(receipts, null, 2));
  return { code, stdout, stderr };
}
const otlp = createServer(async (req, res) => {
  const chunks = []; for await (const chunk of req) chunks.push(chunk);
  const body = JSON.parse(Buffer.concat(chunks).toString());
  assert.equal(req.url, '/tracer/v1/traces');
  captures.push(body); res.setHeader('content-type', 'application/json'); res.end('{}');
});
await new Promise(r => otlp.listen(0, '127.0.0.1', r));
env.FI_OTLP_ENDPOINT = `http://127.0.0.1:${otlp.address().port}/tracer/v1/traces`;
let requestNumber = 0;
const retried = new Set();
const compacted = new Set();
const model = createServer(async (req, res) => {
  const chunks = []; for await (const chunk of req) chunks.push(chunk);
  const body = JSON.parse(Buffer.concat(chunks).toString() || '{}');
  models.push({ path: req.url, body });
  requestNumber++;
  if (JSON.stringify(body.messages ?? []).includes('[overflow-once]') && !retried.has('overflow-once')) {
    retried.add('overflow-once'); models.at(-1).rejected = true;
    res.writeHead(400, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ error: { message: 'maximum context length exceeded', type: 'invalid_request_error', code: 'context_length_exceeded' } })); return;
  }
  if (JSON.stringify(body.messages ?? []).includes('[terminal-auth-error]')) {
    res.writeHead(401, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ error: { message: 'Synthetic unauthorized', type: 'authentication_error' } })); return;
  }
  const tools = body.tools ?? [];
  const read = tools.find(t => t.function?.name === 'read');
  const subagent = tools.find(t => t.function?.name === 'subagent');
  const latest = JSON.stringify((body.messages ?? []).filter(m => m.role === 'user').at(-1));
  if (latest?.includes('[retry]') && !retried.has(latest)) {
    retried.add(latest); res.writeHead(503, { 'content-type': 'application/json', 'retry-after': '0' });
    res.end(JSON.stringify({ error: { message: 'Synthetic retry', type: 'server_error' } })); return;
  }
  const lastUserIndex = (body.messages ?? []).findLastIndex(m => m.role === 'user');
  const hasResult = (body.messages ?? []).slice(lastUserIndex + 1).some(m => m.role === 'tool');
  const tool = (latest?.includes('[foreground-to-background]') || latest?.includes('[subagent]') || latest?.includes('[background-subagent]') || latest?.includes('[resume-subagent:')) && subagent ? subagent : read;
  const input = tool === subagent ? { agent: 'general', description: 'Read public sample', prompt: (latest?.includes('[foreground-to-background]') || latest?.includes('[background-subagent]')) ? '[slow-child] Read sample.txt and explain it.' : 'Read sample.txt and explain it.', ...(latest?.includes('[background-subagent]') ? {background:true} : {}), ...(latest?.match(/\[resume-subagent:([^\]]+)\]/)?.[1] ? {sessionID:latest.match(/\[resume-subagent:([^\]]+)\]/)[1]} : {}) } :
    { path: join(project, latest?.includes('[tool-failure]') ? 'missing.txt' : 'sample.txt') };
  const summary = latest?.includes('structured summary') || latest?.includes('summary template');
  models.at(-1).summary = summary;
  const completion = summary ? { role: 'assistant', content: '## Objective\n- Explain the public sample.\n\n## Requirements\n- (none)\n\n## Decisions\n- Read the sample file.\n\n## Work State\n### Completed\n- Read the public sample.\n### Active\n- Explain the result.\n### Blocked\n- (none)\n\n## Next Move\n1. Answer the user.\n\n## Relevant Files\n- /work/sample/sample.txt: Public sample.\n\n## Important Context\n- The sample contains public text.' } : tool && !hasResult ? { role: 'assistant', content: null,
    tool_calls: [{ id: `call_${requestNumber}`, type: 'function', function: { name: tool.function.name, arguments: JSON.stringify(input) } }] }
    : { role: 'assistant', content: 'The sample file contains public text.' };
  if (summary && JSON.stringify(body.messages).includes('[summary-retry]') && !retried.has('summary-template')) {
    retried.add('summary-template');
    completion.content = 'Invalid summary format forces the real native template retry.';
    models.at(-1).templateRetry = true;
  }
  if (latest?.includes('[interrupt]') || latest?.includes('[slow-child]')) await new Promise(r => setTimeout(r, 2500));
  const forceCompact = latest?.includes('[auto-compact]') && !compacted.has(latest);
  if (forceCompact) compacted.add(latest);
  const usage = { prompt_tokens: forceCompact ? 99999 : 21, completion_tokens: 7, total_tokens: forceCompact ? 100006 : 28 };
  if (body.stream) {
    res.setHeader('content-type', 'text/event-stream');
    const delta = completion.tool_calls ? { role: 'assistant', tool_calls: completion.tool_calls.map((t, index) => ({ index, ...t })) } : completion;
    for (const part of [{ choices: [{ index: 0, delta, finish_reason: null }] },
      { choices: [{ index: 0, delta: {}, finish_reason: completion.tool_calls ? 'tool_calls' : 'stop' }], usage }])
      res.write(`data: ${JSON.stringify({ id: `chatcmpl-${requestNumber}`, object: 'chat.completion.chunk', created: 123456, model: 'test', ...part })}\n\n`);
    res.end('data: [DONE]\n\n');
  } else {
    res.setHeader('content-type', 'application/json');
    res.end(JSON.stringify({ id: `chatcmpl-${requestNumber}`, object: 'chat.completion', created: 123456,
      model: 'test', choices: [{ index: 0, message: completion, finish_reason: completion.tool_calls ? 'tool_calls' : 'stop' }], usage }));
  }
});
await new Promise(r => model.listen(0, '127.0.0.1', r));

// A temporary npm registry serves the exact tarballs, never public @traceai releases.
const tarballs = new Map();
for (const file of await readdir('/work/packs')) {
  if (!file.endsWith('.tgz')) continue;
  // Manifest inventory was generated by the build script, not inferred from tarball filenames.
  const manifest = JSON.parse(await readFile(`/work/packs/${file}.json`, 'utf8'));
  const bytes = await readFile(`/work/packs/${file}`);
  tarballs.set(manifest.name, { manifest, bytes, file });
}
let registry;
registry = createServer(async (req, res) => {
  try {
    const name = decodeURIComponent(req.url.split('?')[0].slice(1));
    const item = tarballs.get(name);
    if (item) {
      const m = { ...item.manifest, dist: { tarball: `http://127.0.0.1:${registry.address().port}/tarballs/${item.file}`,
        shasum: createHash('sha1').update(item.bytes).digest('hex') } };
      res.setHeader('content-type', 'application/json'); res.end(JSON.stringify({ name, 'dist-tags': { latest: m.version }, versions: { [m.version]: m } }));
    } else if (name.startsWith('tarballs/')) {
      const value = [...tarballs.values()].find(v => v.file === name.slice(9));
      if (!value) { res.writeHead(404).end(); return; }
      res.setHeader('content-type', 'application/octet-stream'); res.end(value.bytes);
    } else {
      const upstream = await fetch(`https://registry.npmjs.org/${name}`);
      res.writeHead(upstream.status, { 'content-type': upstream.headers.get('content-type') ?? 'application/json' });
      res.end(Buffer.from(await upstream.arrayBuffer()));
    }
  } catch { res.writeHead(500).end(); }
});
await new Promise(r => registry.listen(0, '127.0.0.1', r));
env.npm_config_registry = `http://127.0.0.1:${registry.address().port}`;
await writeFile(join(home, '.npmrc'), `registry=${env.npm_config_registry}\n`);
await mkdir('/work/event-probe', { recursive: true });
await writeFile('/work/event-probe/package.json', JSON.stringify({ name: 'event-probe', type: 'module', main: './index.js' }));
await writeFile('/work/event-probe/index.js', `import { appendFile } from 'node:fs/promises';
export default { id: 'test.event-probe', async setup(ctx) {
  const controller = new AbortController();
  await appendFile('/work/evidence/events.jsonl', JSON.stringify({type:'test.setup', version:ctx.app.version,location:ctx.location})+'\\n');
  const task = (async () => { try { for await (const e of ctx.event.subscribe({signal:controller.signal})) {
    if(e.type.startsWith('session.')) await appendFile('/work/evidence/events.jsonl',JSON.stringify(e)+'\\n');
  }} catch {} })();
  return async () => {controller.abort(); await task;};
}};`);
await writeFile(join(home, '.config/opencode/opencode.json'), JSON.stringify({
  plugins: ['/work/event-probe'],
  providers: { mock: { name: 'Mock', env: ['MOCK_API_KEY'], package: '@opencode/ai/providers/openai-compatible',
    settings: { baseURL: `http://127.0.0.1:${model.address().port}/v1` }, models: { test: { name: 'Test', limit: { context: 100000, output: 4096 } } } } },
  model: 'mock/test'
}, null, 2));
try {
  env.PATH = `/work/host/node_modules/.bin:${env.PATH}`;
  console.log('VERSION', await run(binary, ['--version']));
  const setup = await run('npx', ['--yes', '@traceai/agents@0.1.0', 'setup', 'opencode', '--project', 'coding-agents', '--json']);
  console.log('ONE_COMMAND_SETUP', setup);
  assert.equal(setup.code, 0);
  assert.equal(JSON.parse(setup.stdout.trim()).registered, true);
  delete env.FI_API_KEY; delete env.FI_SECRET_KEY;
  const repeated = await run('npx', ['--yes', '@traceai/agents@0.1.0', 'setup', 'opencode', '--project', 'coding-agents', '--json']);
  assert.equal(repeated.code, 0);
  // Native alternative remains idempotent and is exercised separately.
  console.log('INSTALL', await run(binary, ['plugin', 'add', '@traceai/opencode-plugin@0.1.0']));
  await writeFile(join(evidence, 'host-config.json'), await readFile(join(home, '.config/opencode/opencode.json')));
  console.log('PATHS', await run(binary, ['debug', 'paths']));
  console.log('LIST', await run(binary, ['plugin', 'list']));
  console.log('CHECK', await run(binary, ['plugin', 'check']));
  // Dependency preparation is complete. Block new non-loopback egress at the sandbox kernel.
  for (const family of ['iptables', 'ip6tables']) {
    execFileSync(family, ['-N', 'FI_AGENT_TEST']);
    execFileSync(family, ['-A', 'FI_AGENT_TEST', '-o', 'lo', '-j', 'ACCEPT']);
    execFileSync(family, ['-A', 'FI_AGENT_TEST', '-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED', '-j', 'ACCEPT']);
    execFileSync(family, ['-A', 'FI_AGENT_TEST', '-j', 'REJECT']);
    execFileSync(family, ['-I', 'OUTPUT', '1', '-j', 'FI_AGENT_TEST']);
  }
  await writeFile(join(evidence, 'egress.json'), JSON.stringify({ mode: 'kernel-new-connections-loopback-only', ipv4: true, ipv6: true }));
  const result = await run(binary, ['--standalone', '--print-logs', 'run', '--format', 'json', '--model', 'mock/test', '--title', 'Public sample', 'Read sample.txt and summarize it.']);
  console.log('RUN', result);
  await new Promise(r => setTimeout(r, 1500));
  const spans = captures.flatMap(r => r.resourceSpans.flatMap(rs => rs.scopeSpans.flatMap(s => s.spans)));
  await writeFile(join(evidence, 'otlp.json'), JSON.stringify(captures, null, 2));
  await writeFile(join(evidence, 'model.json'), JSON.stringify(models, null, 2));
  console.log('CAPTURE', JSON.stringify({ requests: models.length, spans: spans.map(s => ({ name: s.name, parent: s.parentSpanId, status: s.status, attributeKeys: s.attributes.map(a => a.key) })) }));
  assert.equal(result.code, 0);
  assert.ok(models.length >= 2, 'real model/tool loop');
  assert.ok(spans.length >= 4, 'root + two generations + tool');
  const status = await run(process.execPath, ['/work/packages/traceai_agents/dist/cli.js', 'status', 'opencode', '--check', '--json']);
  assert.equal(status.code, 0);
  const inspected = JSON.parse(status.stdout.trim());
  assert.equal(inspected.hostVersion, '2.2.5');
  assert.equal(inspected.activation, 'OBSERVED');
  assert.ok(inspected.delivery.exported >= 4);
  assert.ok(!status.stdout.includes('test-api-key') && !status.stdout.includes('test-secret-key'));
  await writeFile(join(evidence, 'status.json'), JSON.stringify(inspected, null, 2));
  const { scenarios } = await import(process.env.FI_TEST_OUTPUT_PRIVACY === '1' ? './live-privacy.mjs' : './live-scenarios.mjs');
  await scenarios({ binary, env, project, captures, models, run });
} finally {
  await writeFile(join(evidence, 'otlp.json'), JSON.stringify(captures, null, 2));
  await writeFile(join(evidence, 'model.json'), JSON.stringify(models, null, 2));
  for (const family of ['iptables', 'ip6tables']) {
    try { execFileSync(family, ['-D', 'OUTPUT', '-j', 'FI_AGENT_TEST']); execFileSync(family, ['-F', 'FI_AGENT_TEST']); execFileSync(family, ['-X', 'FI_AGENT_TEST']); } catch {}
  }
  registry.closeAllConnections(); registry.close(); model.closeAllConnections(); model.close(); otlp.closeAllConnections(); otlp.close();
}
