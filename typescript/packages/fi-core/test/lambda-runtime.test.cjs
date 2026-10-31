const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { spawn, spawnSync } = require('node:child_process');
const { createRequire } = require('node:module');
const packageJson = process.env.FI_CORE_PACKAGE_JSON;
if (!packageJson) throw new Error('FI_CORE_PACKAGE_JSON must point at a packed test fixture');
const load = createRequire(packageJson);
const fixture = path.resolve(path.dirname(packageJson), '../../..');
const proto = load('@opentelemetry/otlp-transformer/build/src/generated/root').opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
const source = fs.readFileSync(path.join(__dirname, 'lambda-handler.ts'), 'utf8');
const directory = fs.mkdtempSync(path.join(fixture, 'lambda-runtime-'));
for (const extension of ['cts', 'mts']) fs.writeFileSync(path.join(directory, `handler.${extension}`), source);
fs.writeFileSync(path.join(directory, 'types.cts'), `
import type { Handler, Context } from 'aws-lambda';
import { traceLambdaHandler, type LambdaCallback } from '@traceai/fi-core';
const promise: Handler = traceLambdaHandler(async (_event: unknown, _context: Context) => ({ statusCode: 200 }), {});
const callback: Handler = traceLambdaHandler((_event: unknown, _context: Context, cb: LambdaCallback<number>) => cb(null, 1), { completion: 'callback' });
void promise; void callback;
`);
const compile = spawnSync(process.execPath, [load.resolve('typescript/bin/tsc'), '--module', 'NodeNext', '--moduleResolution', 'NodeNext', '--target', 'ES2022', '--strict', '--skipLibCheck', '--types', 'node', '--outDir', path.join(directory, 'out'), ...['handler.cts', 'handler.mts', 'types.cts'].map(f => path.join(directory, f))], { cwd: fixture, encoding: 'utf8' });
assert.equal(compile.status, 0, compile.stdout + compile.stderr);

for (const format of ['cjs', 'mjs']) for (const mode of ['ready', 'no-api-key', 'no-secret-key', 'no-project', 'invalid-url']) {
  test(`customer snippet via local runtime interface: ${format}, ${mode}`, async () => {
    const requests = [], responses = [];
    let nextId = 0;
    const server = http.createServer((req, res) => {
      if (req.url === '/2018-06-01/runtime/invocation/next') {
        if (nextId === 2) { res.writeHead(410); res.end(); return; }
        const id = String(++nextId);
        res.writeHead(200, { 'lambda-runtime-aws-request-id': id, 'lambda-runtime-deadline-ms': String(Date.now() + 5000), 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ body: 'PRIVATE_BODY', requestContext: { http: {} }, headers: { authorization: 'PRIVATE_HEADER' } }));
        return;
      }
      const chunks = [];
      req.on('data', chunk => chunks.push(chunk));
      req.on('end', () => {
        const bytes = Buffer.concat(chunks);
        if (req.url === '/tracer/v1/traces') requests.push({ path: req.url, headers: req.headers, bytes, data: proto.toObject(proto.decode(bytes), { bytes: String, longs: String }) });
        else responses.push({ path: req.url, data: JSON.parse(bytes.toString()) });
        res.writeHead(200); res.end();
      });
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const base = `http://127.0.0.1:${server.address().port}`;
    const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(FI_|OTEL_)/.test(key)));
    Object.assign(env, { FI_API_KEY: 'synthetic-api-secret', FI_SECRET_KEY: 'synthetic-secret-secret', FI_PROJECT_NAME: 'lambda-customer-snippet', OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: base + '/tracer/v1/traces' });
    if (mode === 'no-api-key') delete env.FI_API_KEY;
    if (mode === 'no-secret-key') delete env.FI_SECRET_KEY;
    if (mode === 'no-project') delete env.FI_PROJECT_NAME;
    if (mode === 'invalid-url') env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT = 'not a URL';
    try {
      const output = await new Promise((resolve, reject) => {
        const child = spawn(process.execPath, [path.join(__dirname, 'lambda-runtime-client.cjs'), path.join(directory, `out/handler.${format}`), base], { env });
        let output = '';
        child.stdout.on('data', chunk => { output += chunk; }); child.stderr.on('data', chunk => { output += chunk; });
        const timer = setTimeout(() => { child.kill(); reject(new Error('Runtime harness exceeded 10 seconds')); }, 10000);
        child.on('error', reject);
        child.on('exit', code => { clearTimeout(timer); code === 0 ? resolve(output) : reject(new Error(`Runtime exited ${code}: ${output}`)); });
      });
      assert.equal(output.includes('synthetic-api-secret'), false);
      assert.equal(output.includes('synthetic-secret-secret'), false);
      assert.equal(responses.length, 2);
      for (const response of responses) {
        assert.ok(response.path.endsWith('/response'));
        assert.deepEqual(response.data, { statusCode: 200, body: '{"message":"Ready"}' });
      }
      if (mode !== 'ready') { assert.equal(requests.length, 0); return; }
      assert.equal(requests.length, 2);
      const roots = [];
      for (const request of requests) {
        assert.equal(request.path, '/tracer/v1/traces');
        assert.equal(request.headers['x-api-key'], 'synthetic-api-secret');
        assert.equal(request.headers['x-secret-key'], 'synthetic-secret-secret');
        assert.equal(request.bytes.toString().includes('PRIVATE_'), false);
        const resource = request.data.resourceSpans[0];
        const attrs = Object.fromEntries(resource.resource.attributes.map(a => [a.key, a.value.stringValue]));
        assert.equal(attrs.project_name, 'lambda-customer-snippet'); assert.equal(attrs.project_type, 'observe');
        const spans = resource.scopeSpans.flatMap(s => s.spans);
        assert.equal(spans.length, 2);
        const root = spans.find(s => s.name === 'aws.lambda.invoke'); roots.push(root);
        const child = spans.find(s => s.name === 'prepare-response');
        assert.equal(child.parentSpanId, root.spanId); assert.equal(child.traceId, root.traceId);
      }
      assert.notEqual(roots[0].traceId, roots[1].traceId);
      assert.deepEqual(roots.map(s => s.attributes.find(a => a.key === 'faas.coldstart').value.boolValue), [true, false]);
    } finally {
      server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    }
  });
}
