const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const core = require('../dist/src/index.js');

test('all five OTLP content surfaces retain readable JSON within 20,000 UTF-8 bytes', async t => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'fi-content-wire-'));
  const bodies = [];
  const server = http.createServer((req, res) => {
    // Buffer chunks, not string concatenation: a TCP chunk may bisect a UTF-8 code point.
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => {
      bodies.push(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      res.setHeader('content-type', 'application/json'); res.end('{}');
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const config = await core.resolveConfig({ agent: 'pi', home, env: {
    FI_AGENT_ENABLED: 'true', FI_API_KEY: 'synthetic-content-api', FI_SECRET_KEY: 'synthetic-content-secret',
    FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/v1/traces`,
  } });
  const tracer = core.createTurnTracer({ config });
  t.after(async () => {
    await tracer.shutdown(); server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    await fs.rm(home, { recursive: true, force: true });
  });
  for (const line of ['PUBLIC_RESULT ordinary stdout\n', 'PUBLIC_RESULT 漢字測試 😀\n']) {
    const text = JSON.stringify({ stdout: line.repeat(1100), exitCode: 0 });
    const at = bodies.length;
    const turn = tracer.startTurn({ sessionId: 'readable', turnId: line, input: text });
    turn.event({ name: 'input', input: text });
    turn.child({ id: 'tool', kind: 'tool', name: 'read', input: text }).end({ output: text });
    turn.end({ output: text });
    await tracer.flush();
    const spans = bodies.slice(at).flatMap(b => b.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
    const content = spans.flatMap(s => [...s.attributes, ...(s.events ?? []).flatMap(e => e.attributes)])
      .filter(a => a.key === 'input.value' || a.key === 'output.value').map(a => a.value.stringValue);
    assert.equal(spans.length, 2); assert.equal(content.length, 5);
    for (const value of content) {
      assert.match(value, /PUBLIC_RESULT/);
      assert.doesNotMatch(value, /�|<omitted: incomplete audio>/);
      assert.doesNotThrow(() => JSON.parse(value));
      assert.ok(Buffer.byteLength(value) <= 20000);
      assert.ok(Buffer.byteLength(value) > 19000);
    }
  }
});
