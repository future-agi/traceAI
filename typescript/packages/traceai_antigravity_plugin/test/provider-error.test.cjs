const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const {spawn} = require('node:child_process');
const core = require('@traceai/agent-core');
const {antigravityAdapter} = require(process.env.AGY_PACKAGE_PATH || '../dist');
const attrs = s => Object.fromEntries(s.attributes.map(a => [a.key, Object.values(a.value)[0]]));

test('real agy provider failure remains ERROR without PostInvocation',
  {skip: process.env.AGY_TEST_GENERATION !== '1'}, async t => {
  // Invoked only inside unshare --net with loopback up. No external provider access.
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'fi-agy-provider-error-'));
  const project = path.join(home, 'project');
  await fs.mkdir(project, {mode: 0o700});
  let modelRequests = 0;
  const exports = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', b => chunks.push(b));
    req.on('end', () => {
      res.setHeader('Content-Type', 'application/json');
      if (req.url === '/tracer/v1/traces') {
        exports.push(JSON.parse(Buffer.concat(chunks).toString() || '{}'));
        res.end('{}');
      } else {
        modelRequests++;
        res.statusCode = 400;
        res.end(JSON.stringify({error: {code: 400, status: 'INVALID_ARGUMENT', message: 'Synthetic invalid model request'}}));
      }
    });
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise(r => server.close(r));
    await fs.rm(home, {recursive: true, force: true});
  });
  const base = `http://127.0.0.1:${server.address().port}`;
  const setup = await core.setup({agent: 'antigravity', home, env: {},
    credentials: {apiKey: 'synthetic-api', secretKey: 'synthetic-secret'}, projectName: 'test',
    otlpEndpoint: base + '/tracer/v1/traces', adapter: antigravityAdapter});
  assert.equal(setup.registered, true, JSON.stringify(setup));
  const run = await new Promise((resolve, reject) => {
    const child = spawn('agy', ['--settings', JSON.stringify({modelProvider: 'gemini'}),
      '--model', 'Gemini 4 Argon (Medium)', '--print-timeout', '10s', '--output-format', 'stream-json', '-p', 'Say hello.'],
    {cwd: project, env: {HOME: home, PATH: process.env.PATH, GEMINI_API_KEY: 'synthetic-key',
      GOOGLE_GEMINI_BASE_URL: base, NO_COLOR: '1'}, stdio: ['ignore', 'pipe', 'pipe']});
    let stdout = '', stderr = '';
    child.stdout.on('data', b => stdout += b); child.stderr.on('data', b => stderr += b);
    child.on('error', reject);
    const timer = setTimeout(() => child.kill('SIGKILL'), 20000);
    child.on('close', (code, signal) => {clearTimeout(timer); resolve({code, signal, stdout, stderr});});
  });
  assert.equal(run.signal, null);
  assert.equal(run.code, 3, run.stderr);
  assert.ok(modelRequests > 0);
  const spans = exports.flatMap(b => (b.resourceSpans ?? []).flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
  const roots = spans.filter(s => !s.parentSpanId);
  assert.equal(roots.length, 1);
  assert.equal(roots[0].status.code, 2, JSON.stringify(spans));
  assert.equal(attrs(roots[0])['fi.agent.outcome'], 'error');
  assert.equal(attrs(roots[0])['fi.agent.gap'], 'MISSING_CHILD_STOP');
  assert.ok(spans.some(s => s.parentSpanId && attrs(s)['fi.agent.outcome'] === 'incomplete'));
  assert.equal(spans.filter(s => attrs(s)['fi.span.kind'] === 'LLM').length, 0, 'Do not invent a provider generation');
  if (process.env.AGY_EVIDENCE_DIR) {
    await fs.mkdir(process.env.AGY_EVIDENCE_DIR, {recursive: true});
    await fs.writeFile(path.join(process.env.AGY_EVIDENCE_DIR, 'provider-error.json'), JSON.stringify({modelRequests, run, spans}, null, 2));
  }
  console.log('REAL_PROVIDER_ERROR', JSON.stringify({host: '1.5.2', modelRequests, exitCode: run.code,
    rootStatus: roots[0].status.code, outcome: attrs(roots[0])['fi.agent.outcome'], gap: attrs(roots[0])['fi.agent.gap']}));
});
