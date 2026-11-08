const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const core = require('../dist/src/index.js');
const { registerSecret } = require('@traceai/fi-core');

async function collector(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'fi-content-final-'));
  const bodies = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => {
      bodies.push(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      res.setHeader('content-type', 'application/json'); res.end('{}');
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const config = await core.resolveConfig({ agent: 'pi', home, env: {
    FI_AGENT_ENABLED: 'true', FI_API_KEY: 'synthetic-final-api', FI_SECRET_KEY: 'synthetic-final-secret',
    FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/v1/traces`,
  } });
  const tracer = core.createTurnTracer({ config });
  t.after(async () => {
    await tracer.shutdown(); server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    await fs.rm(home, { recursive: true, force: true });
  });
  return { tracer, async read() {
    await tracer.flush();
    const spans = bodies.splice(0).flatMap(b => b.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
    const content = spans.flatMap(s => [...s.attributes, ...(s.events ?? []).flatMap(e => e.attributes)])
      .filter(a => a.key === 'input.value' || a.key === 'output.value').map(a => a.value.stringValue);
    assert.equal(spans.length, 2);
    assert.equal(content.length, 5);
    return content;
  } };
}

for (const shape of ['json', 'encoded', 'prose', 'short']) {
  test(`late registration cannot expose a credential prefix on any OTLP surface: ${shape}`, async t => {
    const { tracer, read } = await collector(t);
    const prefix = `LATE_BOUNDARY_PRIVATE_${shape}_`;
    const secret = prefix + 'q'.repeat(140);
    let input = 'PUBLIC_RESULT ' + (shape === 'short' ? '' : 'p '.repeat(9940)) + secret + ' tail';
    if (shape === 'json' || shape === 'short') input = JSON.stringify({ stdout: input });
    if (shape === 'encoded') input = JSON.stringify({ encoded: JSON.stringify({ stdout: input }) });
    const turn = tracer.startTurn({ sessionId: 'late', turnId: shape, input });
    turn.event({ name: 'input', input });
    const tool = turn.child({ id: 'tool', kind: 'tool', name: 'read', input });
    registerSecret(secret);
    tool.end({ output: input }); turn.end({ output: input });
    for (const value of await read()) {
      assert.ok(!value.includes(prefix), 'registered credential prefix escaped');
      assert.match(value, /PUBLIC_RESULT/);
      assert.ok(Buffer.byteLength(value) <= 20000);
    }
  });
}

test('deferred model output is re-scrubbed before turn-owned span finalization', async t => {
  const { tracer, read } = await collector(t);
  const prefix = 'LATE_DEFERRED_PRIVATE_';
  const secret = prefix + 'q'.repeat(140);
  const input = JSON.stringify({ stdout: 'PUBLIC_RESULT ' + 'p '.repeat(9940) + secret + ' tail' });
  const turn = tracer.startTurn({ sessionId: 'late', turnId: 'deferred', input, usagePlan: 'turn' });
  turn.event({ name: 'input', input });
  turn.child({ id: 'model', kind: 'llm', fidelity: 'native-request', input }).end({ output: input });
  registerSecret(secret);
  turn.end({ output: input });
  for (const value of await read()) {
    assert.ok(!value.includes(prefix), 'deferred credential prefix escaped');
    assert.match(value, /PUBLIC_RESULT/);
  }
});

test('capture-time environment credentials stay protected after removal before end', async t => {
  const { tracer, read } = await collector(t);
  const prefix = 'CAPTURE_ENV_PRIVATE_';
  const secret = prefix + 'q'.repeat(140);
  const old = process.env.TEST_PRIVATE_KEY;
  t.after(() => { if (old === undefined) delete process.env.TEST_PRIVATE_KEY; else process.env.TEST_PRIVATE_KEY = old; });
  process.env.TEST_PRIVATE_KEY = secret;
  const input = JSON.stringify({ stdout: 'PUBLIC_RESULT ' + 'p '.repeat(9940) + secret + ' tail' });
  const turn = tracer.startTurn({ sessionId: 'late', turnId: 'environment', input });
  turn.event({ name: 'input', input });
  const tool = turn.child({ id: 'tool', kind: 'tool', input });
  delete process.env.TEST_PRIVATE_KEY;
  tool.end({ output: input }); turn.end({ output: input });
  for (const value of await read()) {
    assert.ok(!value.includes(prefix));
    assert.match(value, /PUBLIC_RESULT/);
  }
});

for (const punctuation of ['[PASSWORD]', '{PASSWORD}', '"PASSWORD"', '[1,2]', '{"x":"y"}']) {
  for (const shape of ['sensitive-field', 'ordinary-field', 'encoded', 'prose']) {
    for (const large of [false, true]) {
      test(`punctuation credential retains unrelated public OTLP content: ${punctuation} ${shape} large=${large}`, async t => {
        const secret = `SYNTHETIC_NORMAL_${punctuation}_END`;
        const old = process.env.DATABASE_PASSWORD;
        process.env.DATABASE_PASSWORD = secret;
        t.after(() => { if (old === undefined) delete process.env.DATABASE_PASSWORD; else process.env.DATABASE_PASSWORD = old; });
        const { tracer, read } = await collector(t);
        const pub = 'PUBLIC_EXPECTED_TOOL_RESULT';
        const stdout = large ? (pub + ' file contents\n').repeat(800) : pub;
        let input = JSON.stringify({ stdout, [shape === 'sensitive-field' ? 'password' : 'echo']: secret, exitCode: 0 });
        if (shape === 'encoded') input = JSON.stringify({ result: input });
        if (shape === 'prose') input = stdout + ' with credential ' + secret + ' then public suffix';
        const turn = tracer.startTurn({ sessionId: 'punctuation', turnId: 'one', input });
        turn.event({ name: 'input', input });
        turn.child({ id: 'tool', kind: 'tool', name: 'read', input }).end({ output: input });
        turn.end({ output: input });
        for (const value of await read()) {
          assert.match(value, /PUBLIC_EXPECTED_TOOL_RESULT/);
          assert.doesNotMatch(value, /SYNTHETIC_NORMAL_|PASSWORD/);
          assert.ok(Buffer.byteLength(value) <= 20000);
          if (shape !== 'prose') assert.doesNotThrow(() => JSON.parse(value));
        }
      });
    }
  }
}
