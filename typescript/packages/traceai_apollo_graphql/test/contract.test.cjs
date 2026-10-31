const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { spawn, spawnSync } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const attrs = values => Object.fromEntries((values ?? []).map(a => [a.key,
  a.value.stringValue ?? a.value.intValue ?? a.value.boolValue]));

async function collector(status = 200) {
  const requests = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => {
      requests.push({ headers: req.headers, path: req.url, body: JSON.parse(Buffer.concat(chunks)) });
      res.writeHead(status, { 'content-type': 'application/json' }); res.end('{}');
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return {
    requests, url: `http://127.0.0.1:${server.address().port}`,
    close: () => new Promise(resolve => server.close(resolve)),
    spans: () => requests.flatMap(r => r.body.resourceSpans.flatMap(rs =>
      rs.scopeSpans.flatMap(ss => ss.spans.map(s => ({ ...s,
        attrs: attrs(s.attributes), scope: ss.scope.name, resource: attrs(rs.resource.attributes) }))))),
  };
}
function start(baseUrl, env = {}) {
  const child = spawn(process.execPath, ['--import', path.join(__dirname, env.LEGACY_TRACE_SETUP ? 'legacy-preload.mjs' : 'preload.mjs'), path.join(__dirname, 'scenario.mjs')], {
    cwd: root,
    env: { ...process.env, FI_BASE_URL: baseUrl, FI_API_KEY: 'synthetic-key', FI_SECRET_KEY: 'synthetic-secret',
      FI_PROJECT_NAME: 'apollo-offline', OTEL_RESOURCE_ATTRIBUTES: 'host.name=PRIVATE_HOST',
      OTEL_TRACES_SAMPLER: 'always_on', FI_APOLLO_CAPTURE_CONTENT: 'false', ...env },
  });
  let stdout = '', stderr = '';
  child.stdout.on('data', data => { stdout += data; });
  child.stderr.on('data', data => { stderr += data; });
  const timeout = setTimeout(() => child.kill('SIGKILL'), 15000);
  const done = new Promise((resolve, reject) => {
    child.on('error', reject);
    child.on('close', code => {
      clearTimeout(timeout);
      try { assert.equal(code, 0, stderr); resolve(stdout); } catch (err) { reject(err); }
    });
  });
  return { child, done };
}
const operation = (spans, name) => spans.filter(s => s.name === `query ${name}`);

test('regression control: the prior recipe loses Server 5 HTTP parents', {
  skip: process.env.APOLLO_MAJOR === '4', // Server 4 uses a different HTTP integration.
}, async () => {
  const c = await collector();
  try {
    await start(c.url, { LEGACY_TRACE_SETUP: 'true' }).done;
    const spans = c.spans();
    const op = operation(spans, 'Hello')[0];
    assert.ok(op, 'GraphQL operations still execute and export');
    assert.equal(spans.some(span => span.scope === '@opentelemetry/instrumentation-http' &&
      span.traceId === op.traceId && span.kind === 2), false);
  } finally { await c.close(); }
});

for (const content of [false, true]) test(`real Apollo OTLP wire, captureContent=${content}`, async () => {
  const c = await collector();
  try {
    const stdout = await start(c.url, { FI_APOLLO_CAPTURE_CONTENT: String(content), TEST_FORCE_FLUSH: 'true' }).done;
    const result = JSON.parse(stdout.split('\n').find(line => line.startsWith('RESULT ')).slice(7));
    assert.equal(result.forceFlushed, true);
    assert.equal(result.flushed, true); assert.equal(result.repeatedShutdown, true);
    assert.ok(result.shutdownMs < 3000); assert.match(result.meter, /Noop|Proxy/);
    assert.equal(result.replies.length, 10);
    assert.equal(result.replies[0].data.hello, 'world');
    assert.equal(result.replies[5].errors.length, 1);
    assert.equal(result.replies[6].data.partial.name, 'Ada');
    const spans = c.spans();
    const httpSpans = spans.filter(s => s.scope === '@opentelemetry/instrumentation-http' && s.kind === 2);
    assert.equal(httpSpans.length, 10, 'exactly one HTTP server span per request');
    for (const name of ['Hello', 'Profile', 'Literal', 'Boom', 'Partial', 'Remote']) {
      const ops = operation(spans, name);
      assert.equal(ops.length, ['Hello', 'Profile'].includes(name) ? 2 : 1);
      for (const op of ops) {
        const parent = httpSpans.find(s => s.spanId === op.parentSpanId);
        assert.ok(parent, `${name} must have an HTTP parent`);
        assert.equal(parent.traceId, op.traceId);
        const tree = spans.filter(s => s.traceId === op.traceId);
        assert.equal(tree.filter(s => s.name.startsWith('graphql.resolve')).length, 1);
        if (['Profile', 'Literal'].includes(name)) {
          const resolver = tree.find(s => s.name === 'graphql.resolve profile');
          const child = tree.find(s => s.name === 'load-profile');
          assert.equal(child.parentSpanId, resolver.spanId);
          assert.equal(child.attrs['application.owned'], 'KEEP_APP_ATTRIBUTE');
          assert.ok(BigInt(parent.endTimeUnixNano) - BigInt(parent.startTimeUnixNano) > 150000000n,
            'HTTP root ends with the request, not at a fixed 100ms');
        }
        if (['Boom', 'Partial'].includes(name)) assert.equal(op.status.code, 2);
      }
    }
    const profiles = operation(spans, 'Profile');
    assert.notEqual(profiles[0].traceId, profiles[1].traceId, 'concurrent requests remain isolated');
    const remote = operation(spans, 'Remote')[0];
    assert.equal(remote.traceId, '0123456789abcdef0123456789abcdef');
    assert.equal(httpSpans.find(s => s.traceId === remote.traceId).parentSpanId, '0123456789abcdef');
    const wire = JSON.stringify(c.requests.map(r => r.body));
    for (const secret of ['PRIVATE_VARIABLE', 'PRIVATE_LITERAL', 'PRIVATE_HOST', 'exception.stacktrace', root,
      'url.full', 'server.address', 'user_agent.original', 'process.command_args', 'process.owner']) {
      assert.ok(!wire.includes(secret), `must not export ${secret}`);
    }
    assert.equal(wire.includes('graphql.source'), content);
    assert.equal(wire.includes('PRIVATE_ERROR'), content);
    assert.ok(wire.includes('exception.type'));
    for (const req of c.requests) {
      assert.equal(req.path, '/tracer/v1/traces');
      assert.equal(req.headers['x-api-key'], 'synthetic-key');
      assert.equal(req.headers['x-secret-key'], 'synthetic-secret');
      assert.equal(req.headers['content-encoding'], undefined);
    }
    for (const span of spans) {
      assert.equal(span.resource.project_name, 'apollo-offline');
      assert.equal(span.resource.project_type, 'observe');
      assert.ok(!Object.keys(span.attrs).some(key => key.startsWith('gen_ai.')));
    }
  } finally { await c.close(); }
});

test('SIGTERM drains Apollo before exporting the final batch', async () => {
  const c = await collector();
  try {
    const { child, done } = start(c.url, { SIGNAL_TEST: 'true' });
    const url = await new Promise((resolve, reject) => {
      let output = '';
      child.stdout.on('data', data => {
        output += data;
        const match = output.match(/READY (http:\/\/[^\s]+)/);
        if (match) resolve(match[1]);
      });
      child.on('close', () => reject(new Error('server exited before ready')));
    });
    await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{"query":"{ hello }"}' });
    child.kill('SIGTERM');
    assert.match(await done, /STOP true/);
    assert.ok(c.spans().some(span => span.name === 'query'));
  } finally { await c.close(); }
});

test('exporter rejection is bounded and does not crash the application', async () => {
  const c = await collector(401);
  try {
    const stdout = await start(c.url).done;
    const result = JSON.parse(stdout.split('\n').find(line => line.startsWith('RESULT ')).slice(7));
    assert.equal(result.flushed, false);
    assert.ok(result.shutdownMs < 5000);
    assert.equal(result.replies[0].data.hello, 'world');
  } finally { await c.close(); }
});

test('invalid configuration fails before registering a provider', () => {
  const code = `const assert = require('node:assert/strict'); const {configure}=require('@traceai/apollo-graphql');
    const valid = {apiKey:'synthetic-key', secretKey:'synthetic-secret', baseUrl:'http://127.0.0.1:9'};
    for (const options of [{apiKey:''}, {secretKey:''}, {projectName:' '}, {depth:0}, {depth:1.5},
      {baseUrl:'file:///tmp'}, {baseUrl:'https://user:password@localhost'}, {baseUrl:'http://localhost/?query=private'}]) {
      assert.throws(()=>configure({...valid, ...options}));
    }
    process.env.OTEL_SDK_DISABLED='true'; assert.throws(()=>configure(valid), /OTEL_SDK_DISABLED/);
    delete process.env.OTEL_SDK_DISABLED;
    configure(valid).shutdown();`;
  const result = spawnSync(process.execPath, ['-e', code], { cwd: root, encoding: 'utf8', timeout: 5000 });
  assert.equal(result.status, 0, result.stderr);
});

test('configuration rejects duplicate providers and late initialization without double spans', () => {
  const env = { ...process.env, FI_API_KEY: 'k', FI_SECRET_KEY: 's', FI_BASE_URL: 'http://127.0.0.1:9' };
  const run = code => {
    const result = spawnSync(process.execPath, ['-e', code], { cwd: root, env, encoding: 'utf8', timeout: 5000 });
    assert.equal(result.status, 0, result.stderr);
  };
  run(`const assert = require('node:assert/strict'); const {configure}=require('@traceai/apollo-graphql');
    const telemetry=configure(); assert.throws(()=>configure(), /already configured/); telemetry.shutdown();`);
  run(`const assert = require('node:assert/strict'); require('graphql');
    assert.throws(()=>require('@traceai/apollo-graphql').configure(), /before importing/);`);
  run(`const assert = require('node:assert/strict');
    const {trace}=require('@opentelemetry/api'); trace.setGlobalTracerProvider({getTracer(){}});
    assert.throws(()=>require('@traceai/apollo-graphql').configure(), /already registered/);`);
});
