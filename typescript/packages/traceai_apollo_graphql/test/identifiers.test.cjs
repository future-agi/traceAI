const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const markers = ['PRIVATE_OPERATION_ID_28471', 'PRIVATE_ALIAS_ID_91823',
  'PRIVATE_NESTED_ALIAS_71234', 'PRIVATE_LIST_ALIAS_65231', 'PRIVATE_ITEM_ALIAS_36129'];
const attributes = values => Object.fromEntries((values ?? []).map(a => [a.key, a.value.stringValue]));

for (const flag of ['FI_HIDE_INPUTS', 'FI_HIDE_INPUT_TEXT', 'FI_HIDE_INPUT_MESSAGES']) {
  for (const capture of [false, true]) {
    for (const hidden of [false, true]) {
      test(`OTLP identifiers: ${flag}=${hidden}, captureContent=${capture}`, async () => {
        const requests = [];
        const collector = http.createServer((req, res) => {
          const chunks = [];
          req.on('data', chunk => chunks.push(chunk));
          req.on('end', () => {
            requests.push({ path: req.url, headers: req.headers, body: JSON.parse(Buffer.concat(chunks)) });
            res.writeHead(200, { 'content-type': 'application/json' }); res.end('{}');
          });
        });
        await new Promise(resolve => collector.listen(0, '127.0.0.1', resolve));
        let stdout = '', stderr = '';
        try {
          const env = { ...process.env };
          for (const key of Object.keys(env)) {
            if (key.startsWith('FI_HIDE_') || key.startsWith('OTEL_EXPORTER_OTLP') || key === 'FI_PII_REDACTION') delete env[key];
          }
          const child = spawn(process.execPath, [path.join(__dirname, 'identifiers-scenario.cjs')], {
            cwd: path.resolve(__dirname, '..'),
            env: { ...env, FI_API_KEY: 'synthetic-key', FI_SECRET_KEY: 'synthetic-secret',
              FI_BASE_URL: `http://127.0.0.1:${collector.address().port}`, FI_PROJECT_NAME: 'identifiers-offline',
              FI_APOLLO_CAPTURE_CONTENT: String(capture), [flag]: String(hidden) },
          });
          child.stdout.on('data', data => { stdout += data; });
          child.stderr.on('data', data => { stderr += data; });
          const timer = setTimeout(() => child.kill('SIGKILL'), 15000);
          try {
            const code = await new Promise((resolve, reject) => {
              child.on('error', reject); child.on('close', resolve);
            });
            assert.equal(code, 0, stderr);
          } finally { clearTimeout(timer); }
          if (process.env.OTLP_EVIDENCE_DIR) {
            fs.mkdirSync(process.env.OTLP_EVIDENCE_DIR, { recursive: true });
            fs.writeFileSync(path.join(process.env.OTLP_EVIDENCE_DIR, `${flag}-${hidden}-${capture}.json`),
              JSON.stringify({ requests, application: JSON.parse(stdout), stderr }, null, 2));
          }
          assert.ok(requests.length > 0, 'actual OTLP requests received');
          assert.ok(requests.every(r => r.path === '/tracer/v1/traces'));
          const spans = requests.flatMap(r => r.body.resourceSpans.flatMap(rs =>
            rs.scopeSpans.flatMap(ss => ss.spans.map(s => ({ ...s, attrs: attributes(s.attributes), scope: ss.scope.name })))));
          const operations = spans.filter(s => s.attrs['graphql.operation.type'] === 'query');
          assert.equal(operations.length, 2, 'both cached and uncached operations exported');
          // Check the complete received body, including resource fields, events, links and names.
          const wire = JSON.stringify(requests.map(r => r.body));
          for (const marker of markers) assert.equal(wire.includes(marker), !hidden, `wire visibility of ${marker}`);
          for (const op of operations) {
            assert.equal(op.attrs['graphql.operation.name'], hidden ? '__REDACTED__' : markers[0]);
            assert.equal(op.name, hidden ? 'query __REDACTED__' : `query ${markers[0]}`);
            assert.ok(spans.some(s => s.spanId === op.parentSpanId && s.traceId === op.traceId && s.kind === 2),
              'privacy retains the HTTP parent');
          }
          const resolvers = spans.filter(s => s.attrs['graphql.field.path'] !== undefined);
          assert.ok(resolvers.length >= 8, 'nested and list resolver paths exercised');
          for (const resolver of resolvers) {
            if (hidden) {
              assert.equal(resolver.attrs['graphql.field.path'], '__REDACTED__');
              assert.equal(resolver.name, `graphql.resolve ${resolver.attrs['graphql.field.name']}`);
            }
            assert.ok(['profile', 'name', 'items'].includes(resolver.attrs['graphql.field.name']),
              'schema-derived field names remain useful');
          }
          assert.equal(wire.includes('graphql.source'), capture && !hidden);
        } finally { await new Promise(resolve => collector.close(resolve)); }
      });
    }
  }
}
