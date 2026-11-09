// Real Auggie startup is separate evidence from hook replay. Never supplies service auth.
const fs = require('node:fs/promises');
const path = require('node:path');
const http = require('node:http');
const { spawn } = require('node:child_process');
const assert = require('node:assert/strict');
const root = '/work/qualification';
function run(file, args, env, input = '') {
  return new Promise((resolve) => {
    const child = spawn(file, args, { env, cwd: root, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '', stderr = '', timedOut = false;
    const timer = setTimeout(() => { timedOut = true; child.kill('SIGKILL'); }, 20000);
    child.stdout.on('data', (b) => stdout += b); child.stderr.on('data', (b) => stderr += b);
    child.on('error', (e) => { clearTimeout(timer); resolve({ code: null, error: e.code }); });
    child.on('close', (code) => { clearTimeout(timer); resolve({ code, stdout, stderr, timedOut }); });
    child.stdin.end(input);
  });
}
(async () => {
  await fs.mkdir(root, { recursive: true, mode: 0o700 });
  const home = path.join(root, 'fresh home');
  await fs.mkdir(home, { mode: 0o700 });
  const exports = [], model = [];
  const server = http.createServer((req, res) => {
    let data = ''; req.on('data', (b) => data += b); req.on('end', () => {
      if (req.url === '/tracer/v1/traces') { exports.push(JSON.parse(data)); res.writeHead(200, { 'content-type': 'application/json' }); res.end('{}'); }
      else { model.push({ path: req.url }); res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify({ id: 'mock', object: 'chat.completion', choices: [{ index: 0, message: { role: 'assistant', content: 'Synthetic response' }, finish_reason: 'stop' }] })); }
    });
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  const env = { HOME: home, PATH: `/work/vendor/node_modules/.bin:${process.env.PATH}`, AUGMENT_DISABLE_AUTO_UPDATE: '1',
    FI_API_KEY: 'synthetic-api', FI_SECRET_KEY: 'synthetic-secret', FI_OTLP_ENDPOINT: `${base}/tracer/v1/traces` };
  const cli = '/work/packed/node_modules/@traceai/agents/dist/cli.js';
  const setup = await run(process.execPath, [cli, 'setup', 'augment', '--project', 'packed-e2e', '--json'], env);
  assert.equal(setup.code, 0, setup.stderr + setup.stdout); assert.equal(JSON.parse(setup.stdout).registered, true);
  const repeat = await run(process.execPath, [cli, 'setup', 'augment', '--project', 'packed-e2e', '--json'], env);
  assert.equal(repeat.code, 0);
  const settings = JSON.parse(await fs.readFile(path.join(home, '.augment/settings.json'), 'utf8'));
  assert.equal(settings.hooks.Stop.length, 1);
  assert.equal((await fs.stat(path.join(home, '.config/futureagi/agents/credentials.json'))).mode & 0o777, 0o600);
  const hookEnv = { HOME: home, PATH: '', AUGMENT_PROJECT_DIR: root };
  const replay = await run('/bin/sh', ['-c', settings.hooks.Stop[0].hooks[0].command], hookEnv,
    JSON.stringify({ hook_event_name: 'Stop', conversation_id: 'packed-fixture', agent_stop_cause: 'end_turn', conversation: { userPrompt: 'Packed fixture', agentTextResponse: 'Fixture only' } }));
  assert.deepEqual(replay, { code: 0, stdout: '', stderr: '', timedOut: false });
  const spans = () => exports.flatMap((e) => e.resourceSpans ?? []).flatMap((r) => r.scopeSpans ?? []).flatMap((s) => s.spans ?? []);
  assert.equal(spans().length, 1);
  const status = await run(process.execPath, [cli, 'status', 'augment', '--json'], env);
  assert.doesNotMatch(status.stdout, /synthetic-api|synthetic-secret/);
  const before = spans().length;
  // A local OpenAI-compatible server is provided; no undocumented flag is claimed to route Auggie to it.
  // Guard all Node TCP connects during real execution. The run has no service credentials.
  const guarded = { ...env, NODE_OPTIONS: '--require=/work/qualification/loopback-only.cjs',
    OPENAI_BASE_URL: `${base}/v1`, OPENAI_API_KEY: 'synthetic-model-key', HTTP_PROXY: 'http://127.0.0.1:9', HTTPS_PROXY: 'http://127.0.0.1:9' };
  await fs.writeFile('/work/qualification/loopback-only.cjs', `const net=require('node:net');const original=net.Socket.prototype.connect;net.Socket.prototype.connect=function(...a){const options=Array.isArray(a[0])?a[0][0]:a[0];const host=typeof options==='object'?options.host:typeof a[1]==='string'?a[1]:undefined;if(host&&!['127.0.0.1','localhost','::1'].includes(host))throw new Error('NON_LOOPBACK_BLOCKED');return original.apply(this,a);};`);
  const binary = '/work/vendor/node_modules/.bin/auggie';
  const version = await run(binary, ['--version'], guarded);
  assert.match(version.stdout, /0\.37\.0/);
  const actual = await run(binary, ['--print', '--max-turns', '1', '--workspace-root', root, '--instruction', 'Return the text synthetic only.'], guarded);
  assert.doesNotMatch(actual.stderr, /Workspace root error/);
  assert.match(actual.stderr, /No auth provided|Authentication failed/);
  const actualSpans = spans().slice(before);
  const disabled = await run(process.execPath, [cli, 'disable', 'augment', '--json'], env); assert.equal(disabled.code, 0);
  const beforeDisabled = spans().length;
  await run('/bin/sh', ['-c', settings.hooks.Stop[0].hooks[0].command], hookEnv, JSON.stringify({ hook_event_name: 'Stop', conversation_id: 'disabled', agent_stop_cause: 'end_turn' }));
  assert.equal(spans().length, beforeDisabled);
  const uninstall = await run(process.execPath, [cli, 'uninstall', 'augment', '--json'], env); assert.equal(uninstall.code, 0);
  const customDir = path.join(home, 'custom Augment');
  const scopedOptions = ['--scope', 'project', '--project-dir', root, '--agent-dir', customDir, '--json'];
  const projectSetup = await run(process.execPath, [cli, 'setup', 'augment', '--project', 'packed-e2e', ...scopedOptions], env);
  assert.equal(projectSetup.code, 0, projectSetup.stdout + projectSetup.stderr);
  const projectSettings = JSON.parse(await fs.readFile(path.join(root, '.augment/settings.local.json'), 'utf8'));
  assert.equal(projectSettings.hooks.Stop.length, 1);
  const projectUninstall = await run(process.execPath, [cli, 'uninstall', 'augment', ...scopedOptions], env);
  assert.equal(projectUninstall.code, 0);
  const removed = JSON.parse(await fs.readFile(path.join(root, '.augment/settings.local.json'), 'utf8'));
  assert.equal(removed.hooks.Stop.length, 0);
  const report = { status: 'HOLD', projectScopeLifecycle: true, customAgentDirectory: true, version, setup: JSON.parse(setup.stdout), repeatedSetup: JSON.parse(repeat.stdout),
    packedLauncherFixtureSpans: before, realBinary: actual, realBinarySpans: actualSpans.length, mockModelRequests: model.length,
    blocker: 'Auggie requires Augment service authentication; no documented generic local-model endpoint. Fixture replay does not qualify the real binary hook protocol or model loop.',
    disabledNoExport: true, uninstalled: true, statusOutput: JSON.parse(status.stdout) };
  await fs.mkdir('/work/evidence', { recursive: true });
  await fs.writeFile('/work/evidence/real-binary.json', JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  await new Promise((r) => server.close(r));
})().catch((e) => { console.error(e); process.exit(1); });
