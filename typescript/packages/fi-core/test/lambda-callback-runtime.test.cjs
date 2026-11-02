const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { createRequire } = require('node:module');
const load = createRequire(process.env.FI_CORE_PACKAGE_JSON);
const proto = load('@opentelemetry/otlp-transformer/build/src/generated/root').opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;

for (const [mode, outcome] of [['default', 'success'], ['true', 'success'], ['false', 'success'], ['false', 'error'], ['false', 'throw']]) {
  test(`official Node 22 callback runtime with stalled OTLP: ${mode}, ${outcome}`, { skip: Number(process.versions.node.split('.')[0]) !== 22 }, async t => {
    const requests = [];
    const server = http.createServer((req, _res) => {
      const chunks = [];
      req.on('data', chunk => chunks.push(chunk));
      req.on('end', () => requests.push({ path: req.url, headers: req.headers, bytes: Buffer.concat(chunks) }));
      // Keep the real protobuf export socket pending beyond the wrapper's flush budget.
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    try {
      const output = await new Promise((resolve, reject) => {
        const child = spawn(process.execPath, [path.join(__dirname, 'lambda-callback-runtime-client.cjs'),
          `http://127.0.0.1:${server.address().port}/tracer/v1/traces`, mode, outcome], { env: process.env });
        let stdout = '', stderr = '';
        child.stdout.on('data', c => { stdout += c; }); child.stderr.on('data', c => { stderr += c; });
        const timer = setTimeout(() => { child.kill(); reject(new Error('Callback runtime exceeded 5 seconds')); }, 5000);
        child.on('error', error => { clearTimeout(timer); reject(error); });
        child.on('exit', code => { clearTimeout(timer); code === 0 ? resolve(stdout) : reject(new Error(`Child exit ${code}: ${stdout}${stderr}`)); });
      });
      const timing = JSON.parse(output);
      t.diagnostic(JSON.stringify(timing));
      assert.ok(timing.callbackMs < 500);
      assert.ok(timing.postMs < 500);
      assert.equal(requests.length, mode === 'false' ? 1 : 0, 'unsafe callback mode must not trace or export');
      for (const request of requests) {
        assert.equal(request.path, '/tracer/v1/traces');
        assert.equal(request.headers['x-api-key'], 'synthetic-api');
        assert.equal(request.headers['x-secret-key'], 'synthetic-secret');
        assert.equal(request.bytes.toString().includes('PRIVATE_'), false);
        const data = proto.toObject(proto.decode(request.bytes), { bytes: String, longs: String });
        const spans = data.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans));
        assert.equal(spans.length, 1);
        assert.equal(spans[0].name, 'aws.lambda.invoke');
        assert.notEqual(spans[0].endTimeUnixNano, '0');
        if (outcome !== 'success') assert.equal(spans[0].status.code, 2);
      }
    } finally {
      server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    }
  });
}
