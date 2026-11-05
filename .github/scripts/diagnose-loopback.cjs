// Bounded synthetic transport census. No packages, credentials, DNS, or external
// destinations. Every failed operation is retained; no operation is retried.
const assert = require('node:assert/strict');
const http = require('node:http');
const net = require('node:net');
const { once } = require('node:events');

function connect(port, mode) {
  if (mode === 'fetch') {
    return fetch(`http://127.0.0.1:${port}`, { signal: AbortSignal.timeout(3000) })
      .then(async response => assert.equal(await response.text(), 'ok'));
  }
  return new Promise((resolve, reject) => {
    const socket = mode === 'net'
      ? net.connect({ host: '127.0.0.1', port })
      : http.get({ host: '127.0.0.1', port, agent: false }, response => {
        let body = '';
        response.on('data', chunk => { body += chunk; });
        response.on('end', () => {
          try { assert.equal(body, 'ok'); resolve(); } catch (error) { reject(error); }
        });
        response.on('error', reject);
      });
    socket.once('error', reject);
    if (mode === 'net') socket.once('connect', () => { socket.destroy(); resolve(); });
    socket.setTimeout(3000, () => socket.destroy(new Error('loopback timeout')));
  });
}

(async () => {
  const summary = { label: process.argv[2], pid: process.pid, node: process.version, platform: process.platform, passed: 0, failed: 0, byMode: {} };
  for (const mode of ['net', 'http', 'fetch']) {
    summary.byMode[mode] = { passed: 0, failed: 0 };
    for (let iteration = 0; iteration < 300; iteration++) {
      const server = http.createServer((_request, response) => response.end('ok'));
      server.listen(0, '127.0.0.1');
      await once(server, 'listening');
      const port = server.address().port;
      try {
        await connect(port, mode);
        summary.passed++;
        summary.byMode[mode].passed++;
      } catch (error) {
        summary.failed++;
        summary.byMode[mode].failed++;
        const cause = error.cause || error;
        console.log(JSON.stringify({ event: 'failure', label: summary.label, pid: process.pid, mode, iteration, port, code: cause.code, errno: cause.errno, syscall: cause.syscall, address: cause.address, message: cause.message }));
      } finally {
        server.closeAllConnections();
        await new Promise(resolve => server.close(resolve));
      }
    }
  }
  console.log(JSON.stringify({ event: 'summary', ...summary }));
  assert.equal(summary.failed, 0, 'all synthetic loopback operations must succeed');
})().catch(error => { console.error(error); process.exitCode = 1; });
