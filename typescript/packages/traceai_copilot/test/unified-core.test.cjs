const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const core = require('@traceai/agent-core');
const { nativeSpan } = require('../dist/index.js');

test('repository cannot elevate a protected project privacy ceiling through a content-enabled user profile', async t => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'copilot-core-privacy-'));
  const payloads = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => {
      payloads.push(JSON.parse(Buffer.concat(chunks)));
      res.setHeader('content-type', 'application/json');
      res.end('{}');
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    await fs.rm(home, { recursive: true, force: true });
  });
  const options = { agent: 'copilot', home, env: {} };
  const setup = {
    ...options, projectName: 'user-project',
    credentials: { apiKey: 'fixture-api', secretKey: 'fixture-secret' },
    otlpEndpoint: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
    configureOnly: true,
  };
  assert.equal((await core.setup({ ...setup, captureContent: true })).configured, true);
  const projectDir = path.join(home, 'repository');
  await fs.mkdir(projectDir, { mode: 0o700 });
  assert.equal((await core.setup({ ...setup, projectDir, scope: 'project', projectName: 'private-project', captureContent: false })).configured, true);
  const paths = await core.configPaths({ ...options, projectDir });
  const settings = JSON.parse(await fs.readFile(paths.project, 'utf8'));
  const store = JSON.parse(await fs.readFile(paths.credentials, 'utf8'));
  assert.equal(store.profiles[settings.profile].captureContent, false);
  assert.equal(JSON.parse(await fs.readFile(paths.user, 'utf8')).captureContent, true);
  await core.writeProtected(paths.project, JSON.stringify({ ...settings, captureContent: true }));
  const config = await core.resolveConfig({ ...options, projectDir });
  assert.equal(config.enabled, true);
  assert.equal(config.destinationApproved, true);
  assert.equal(config.captureContent, false);
  const privateText = 'PRIVATE_PROJECT_PROMPT_MUST_NOT_LEAVE';
  const span = nativeSpan({
    type: 'span', traceId: 'a'.repeat(32), spanId: 'b'.repeat(16),
    startTime: [1, 0], endTime: [2, 0], status: { code: 0 },
    attributes: { 'gen_ai.operation.name': 'chat', 'gen_ai.input.messages': privateText, 'gen_ai.output.messages': privateText },
  }, config);
  assert.ok(span);
  const processor = new core.BoundedSpanProcessor(config);
  processor.onEnd(span);
  await processor.shutdown();
  const spans = payloads.flatMap(p => p.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
  assert.equal(spans.length, 1, 'policy must redact rather than silently disable valid export');
  assert.ok(!JSON.stringify(payloads).includes(privateText), 'no native or FI alias may leak the prompt');
  const receipt = JSON.parse(await fs.readFile(core.deliveryStatePath(config), 'utf8'));
  assert.equal(receipt.exported, 1);
});
