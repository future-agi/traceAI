const { test } = require('node:test');
const assert = require('node:assert/strict');
const https = require('node:https');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync, spawn } = require('node:child_process');
const { once } = require('node:events');
const { ids, fake } = require('./attachments-fixtures.cjs');

test('Node TLS transfer uses upload capability, bounds readers and does not keep host alive', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'attachment-tls-'));
  const key = path.join(dir, 'test.key'); const cert = path.join(dir, 'test.crt');
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', key, '-out', cert, '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost'], { stdio: 'ignore' });
  const bodies = []; const headers = []; let stalled = false;
  const server = https.createServer({ key: fs.readFileSync(key), cert: fs.readFileSync(cert) }, async (req, res) => {
    headers.push(req.headers); const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks); res.setHeader('content-type', 'application/json');
    if (req.url.endsWith('attachment-sessions')) res.end(JSON.stringify(await fake().transport.bootstrap(JSON.parse(body))));
    else if (req.url.endsWith('/uploads')) { res.statusCode = 201; res.end(JSON.stringify(await fake().transport.initialize(null, JSON.parse(body)))); }
    else if (req.url.endsWith('/content')) {
      bodies.push(body); assert.equal(Number(req.headers['content-length']), body.length);
      if (stalled) return;
      res.end(JSON.stringify({ upload_id: ids[0], state: 'pending_upload', reason: 'queued' }));
    } else { res.statusCode = 202; res.end(JSON.stringify({ upload_id: ids[0], state: 'validating', reason: 'binding_pending' })); }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const modulePath = path.resolve(path.dirname(process.env.FI_CORE_PACKAGE_JSON || path.resolve(__dirname, '../package.json')), 'dist/src/attachments/node.js');
  async function child(stall) {
    const script = `
      const { createNodeAttachmentManager } = require(${JSON.stringify(modulePath)});
      const manager = createNodeAttachmentManager({ enabled: true, projectName: 'fixture', projectType: 'observe',
        endpoint: 'https://127.0.0.1:${server.address().port}', headers: { authorization: 'Bearer synthetic-bootstrap', 'x-api-key': 'synthetic-initial-only' } });
      const live = setTimeout(() => {}, ${stall ? 500 : 10000});
      (async () => {
        if (!await manager.ready()) throw Error('ready');
        const handle = manager.attach({ traceId: '0123456789abcdef0123456789abcdef', spanId: '0123456789abcdef', traceFlags: 1 },
          { kind: 'Blob', data: new Blob([new Uint8Array(700000).fill(7)]) }, { direction: 'mixed', mimeType: 'audio/wav' });
        if (${stall}) { console.log('TRANSFER_STARTED'); return; }
        console.log(JSON.stringify(await handle.completion)); await manager.shutdown(); clearTimeout(live);
      })().catch(() => process.exitCode = 1);
    `;
    const processChild = spawn(process.execPath, ['-e', script], { env: { ...process.env, NODE_EXTRA_CA_CERTS: cert }, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = ''; let stderr = ''; processChild.stdout.on('data', c => { stdout += c; }); processChild.stderr.on('data', c => { stderr += c; });
    const deadline = setTimeout(() => processChild.kill('SIGKILL'), 5000);
    const [code, signal] = await once(processChild, 'exit'); clearTimeout(deadline);
    assert.equal(signal, null, 'background transfer kept the process alive'); assert.equal(code, 0, stderr);
    return stdout;
  }
  try {
    assert.match(await child(false), /"status":"validating"/);
    assert.equal(bodies[0].length, 700000); assert.ok(bodies[0].every(byte => byte === 7));
    assert.equal(headers[2].authorization, 'Bearer synthetic-write-capability'); assert.equal(headers[2]['x-api-key'], undefined);
    assert.equal(headers[3].authorization, 'Bearer synthetic-write-capability');
    stalled = true;
    assert.match(await child(true), /TRANSFER_STARTED/); assert.equal(bodies.length, 2);
  } finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); fs.rmSync(dir, { recursive: true, force: true }); }
});
