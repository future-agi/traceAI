// One fixed, synthetic source-binding comparison. Run only under network.sb.
// The two arms differ ONLY in the explicit client source endpoint. No retries,
// warmup, reused listeners/connections, SDKs, credentials, or policy changes.
const assert = require('node:assert/strict');
const http = require('node:http');
const net = require('node:net');
const { once } = require('node:events');

const arm = process.argv[2];
assert.ok(['baseline', 'source-bound'].includes(arm));
const source = arm === 'source-bound'
  ? { localAddress: '127.0.0.1', localPort: 0 }
  : {};
const summary = {
  event: 'summary', arm, node: process.version, platform: process.platform,
  planned: 300, passed: 0, failed: 0, unreached: 300,
  sourceBoundBeforeConnect: 0, denial: { status: 'unreached' },
};
const errorData = error => ({
  code: error.code, errno: error.errno, syscall: error.syscall,
  address: error.address, port: error.port, message: error.message, stack: error.stack,
});

async function attempt(iteration) {
  const events = [];
  const server = http.createServer((_request, response) => response.end('ok'));
  let teardown = false;
  let socket;
  let failure;
  server.on('connection', peer => events.push({
    event: 'accept', localAddress: peer.localAddress, localPort: peer.localPort,
    remoteAddress: peer.remoteAddress, remotePort: peer.remotePort,
  }));
  try {
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    const listener = server.address();
    events.push({ event: 'listening', listener });
    await new Promise((resolve, reject) => {
      // net.connect itself constructs a Socket then calls connect. Attach the
      // same passive observers before that call in BOTH comparison arms.
      socket = new net.Socket();
      socket.on('connectionAttempt', (ip, port, family) => {
        const local = socket.address();
        if (local.address === '127.0.0.1' && local.port > 0) {
          summary.sourceBoundBeforeConnect++;
        }
        events.push({ event: 'connectionAttempt', ip, port, family, local });
      });
      socket.once('error', error => {
        events.push({ event: 'error', listening: server.listening, teardown,
          listener: server.address(), ...errorData(error) });
        reject(error);
      });
      socket.once('close', hadError => events.push({ event: 'close', hadError }));
      socket.once('connect', () => {
        events.push({ event: 'connect', local: socket.address(),
          remoteAddress: socket.remoteAddress, remotePort: socket.remotePort });
        // Preserve the original raw probe's close scheduling in both arms.
        socket.destroy();
        resolve();
      });
      socket.setTimeout(3000, () => socket.destroy(new Error('loopback timeout')));
      events.push({ event: 'connect-issued', listener, teardown });
      socket.connect({ host: '127.0.0.1', port: listener.port, ...source });
    });
  } catch (error) {
    failure = error;
  } finally {
    teardown = true;
    events.push({ event: 'teardown-start', listening: server.listening });
    if (socket && !socket.destroyed) socket.destroy();
    server.closeAllConnections();
    if (server.listening) await new Promise(resolve => server.close(resolve));
  }
  summary.unreached--;
  if (failure) {
    summary.failed++;
    console.log(JSON.stringify({ event: 'failure', arm, iteration,
      ...errorData(failure), events }));
  } else {
    summary.passed++;
    if (iteration === 0) console.log(JSON.stringify({ event: 'first-success', arm, iteration, events }));
  }
}

(async () => {
  try {
    for (let iteration = 0; iteration < summary.planned; iteration++) {
      await attempt(iteration);
    }
    // TEST-NET, no DNS/payload/credentials. Test the SAME source setup against
    // the unchanged destination-based denial, after all positive observations.
    const result = await new Promise(resolve => {
      const socket = net.connect({ host: '203.0.113.1', port: 443, ...source });
      socket.once('error', error => resolve({ status: 'error', ...errorData(error) }));
      socket.once('connect', () => {
        socket.destroy();
        resolve({ status: 'connected' });
      });
      socket.setTimeout(2000, () => {
        socket.destroy();
        resolve({ status: 'timeout' });
      });
    });
    summary.denial = result;
    assert.equal(result.code, 'EPERM', 'non-loopback must be denied, not time out');
    if (arm === 'source-bound') {
      assert.equal(summary.sourceBoundBeforeConnect, summary.planned,
        'every candidate socket must be source-bound before native connect');
    }
    assert.equal(summary.failed, 0, 'every fresh loopback connection must succeed');
  } finally {
    console.log(JSON.stringify(summary));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
