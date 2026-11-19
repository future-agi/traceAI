const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const packageRoot = path.join(__dirname, '..');

for (const vitest of ['vitest', 'vitest-floor']) test(`real ${vitest} eval metadata exports via OTLP/HTTP`, async () => {
  const requests = [];
  const server = http.createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    requests.push({ path: req.url, headers: req.headers, body: JSON.parse(body) });
    res.writeHead(200, { 'content-type': 'application/json' }); res.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const floor = process.env.TEST_FLOOR_CONSUMER || path.resolve(packageRoot, '../../../../floor-consumer');
    const consumer = vitest === 'vitest-floor' ? floor : packageRoot;
    if (vitest === 'vitest-floor') {
      fs.mkdirSync(path.join(floor, 'test'), { recursive: true });
      fs.copyFileSync(path.join(__dirname, 'eval.fixture.test.mjs'), path.join(floor, 'test/eval.fixture.test.mjs'));
      fs.writeFileSync(path.join(floor, 'vitest.config.mjs'), `import { createRequire } from 'node:module';\nconst require=createRequire(import.meta.url);\nconst { VitestEvalsReporter }=require(process.env.TEST_REPORTER_ENTRY);\nexport default { test: { include: ['test/eval.fixture.test.mjs'], reporters: ['default', new VitestEvalsReporter({ endpoint: process.env.TEST_OTLP_ENDPOINT, apiKey: 'synthetic-key', secretKey: 'synthetic-secret' })] } };\n`);
    }
    const cli = path.join(consumer, 'node_modules/vitest/vitest.mjs');
    const child = spawn(process.execPath, [cli, 'run', '--config', vitest === 'vitest-floor' ? 'vitest.config.mjs' : 'test/vitest.config.mjs'], {
      cwd: consumer,
      env: { ...process.env, TEST_REPORTER_ENTRY: path.join(packageRoot, 'dist/index.js'), TEST_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/v1/traces` },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let output = '';
    for (const stream of [child.stdout, child.stderr]) stream.on('data', chunk => { output += chunk; });
    const code = await new Promise(resolve => child.on('exit', resolve));
    assert.equal(code, 0, output);
    assert.equal(requests.length, 1, output);
    assert.equal(requests[0].path, '/v1/traces');
    assert.equal(requests[0].headers['x-api-key'], 'synthetic-key');
    const json = JSON.stringify(requests[0].body);
    assert.match(json, /model-1/);
    assert.doesNotMatch(json, /private prompt|private answer|private input|synthetic-secret/);
    assert.match(json, /gen_ai.usage.input_tokens/);
  } finally { await new Promise(resolve => server.close(resolve)); }
});
