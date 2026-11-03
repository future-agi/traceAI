const assert = require('node:assert/strict');
const http = require('node:http');
const { spawn } = require('node:child_process');
const path = require('node:path');
(async () => {
  const requests = [];
  const receiver = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => {
      requests.push({ path: req.url, headers: req.headers, body: JSON.parse(Buffer.concat(chunks).toString()) });
      res.setHeader('content-type', 'application/json'); res.end('{}');
    });
  });
  await new Promise(resolve => receiver.listen(0, '127.0.0.1', resolve));
  try {
    let stdout = '', stderr = '';
    const child = spawn(process.execPath, [path.resolve(__dirname, '../examples/local-pipeline.mjs')], {
      env: { ...process.env, FI_API_KEY: 'synthetic-docs-api', FI_SECRET_KEY: 'synthetic-docs-secret',
        FI_TRACE_ENDPOINT: `http://127.0.0.1:${receiver.address().port}/tracer/v1/traces`,
        MODEL_DIRECTORY: process.env.FIXTURES, MODEL_ID: 'bert' },
    });
    child.stdout.on('data', data => { stdout += data; }); child.stderr.on('data', data => { stderr += data; });
    const code = await new Promise((resolve, reject) => { child.on('close', resolve); child.on('error', reject); });
    assert.equal(code, 0, stderr);
    assert.match(stdout, /Embedding shape:/);
    assert.equal(requests.length, 1, 'shutdown must export exactly one invocation');
    const req = requests[0];
    assert.equal(req.path, '/tracer/v1/traces');
    assert.equal(req.headers.fi_api_key, 'synthetic-docs-api'); assert.equal(req.headers.fi_secret_key, 'synthetic-docs-secret');
    const [resource] = req.body.resourceSpans;
    const attrs = Object.fromEntries(resource.resource.attributes.map(a => [a.key, a.value.stringValue]));
    assert.equal(attrs.project_name, 'transformers-example'); assert.equal(attrs.project_type, 'observe');
    const [span] = resource.scopeSpans.flatMap(s => s.spans);
    assert.equal(span.name, 'transformers.feature-extraction');
    assert.equal(span.kind, 1); // OTLP INTERNAL enum (API's internal value is 0).
    assert.equal(span.attributes.find(a => a.key === 'fi.span.kind').value.stringValue, 'EMBEDDING');
    assert.equal(span.attributes.find(a => a.key === 'embedding.model_name').value.stringValue, 'bert');
    assert.ok(BigInt(span.endTimeUnixNano) > BigInt(span.startTimeUnixNano));
    assert.ok(!JSON.stringify(req.body).includes('synthetic-docs-'));
    console.log('PASS exact customer snippet, synthetic FI headers, resource, OTLP/HTTP, flush/shutdown');
  } finally { await new Promise(resolve => receiver.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; });
