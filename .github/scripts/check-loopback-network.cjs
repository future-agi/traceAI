// Exercise real sockets before the SDK suite; never retry a denied connection.
const assert = require('node:assert/strict');
const http = require('node:http');
const net = require('node:net');
const { once } = require('node:events');

(async () => {
  for (let iteration = 0; iteration < 50; iteration++) {
    const server = http.createServer((_request, response) => response.end('ok'));
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    try {
      const response = await fetch(`http://127.0.0.1:${server.address().port}`);
      assert.equal(await response.text(), 'ok');
    } finally {
      server.closeAllConnections();
      await new Promise(resolve => server.close(resolve));
    }
  }
  console.log('PASS 50 fresh loopback HTTP connections');
  if (process.argv.includes('--check-denial')) {
    // Documentation-only TEST-NET address, with no DNS or credentials. The
    // kernel must reject the connection, not merely let it time out.
    const error = await new Promise(resolve => {
      const socket = net.connect({ host: '203.0.113.1', port: 443 });
      socket.once('error', resolve);
      socket.once('connect', () => { socket.destroy(); resolve(null); });
      socket.setTimeout(2000, () => {
        socket.destroy();
        resolve(new Error('non-loopback connection was not denied immediately'));
      });
    });
    assert.equal(error?.code, 'EPERM');
    console.log('PASS non-loopback TCP remains denied with EPERM');
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
