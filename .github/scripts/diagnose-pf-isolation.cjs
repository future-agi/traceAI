// Synthetic-only PF architecture probe. Never invokes providers or reads credentials.
// Also a passive first-connect observer when required by the unchanged census.
'use strict';
const net = require('node:net');
const http = require('node:http');
const dgram = require('node:dgram');
const dns = require('node:dns');
const os = require('node:os');
const fs = require('node:fs');
const { spawnSync } = require('node:child_process');
const { once } = require('node:events');
const emit = data => console.log(JSON.stringify({ uid: process.getuid(), euid: process.geteuid(), pid: process.pid, node: process.version, ...data }));

if (require.main !== module) {
  const connect = net.connect;
  let first = true;
  net.connect = function (...args) {
    const socket = Reflect.apply(connect, this, args);
    if (first) {
      first = false;
      const start = performance.now();
      emit({ event: 'cold-start', host: args[0].host, port: args[0].port });
      socket.once('connect', () => emit({ event: 'cold-result', result: 'connected', milliseconds: performance.now() - start }));
      socket.once('error', error => emit({ event: 'cold-result', result: 'error', code: error.code, milliseconds: performance.now() - start }));
    }
    return socket;
  };
} else {
  main().catch(error => { emit({ event: 'probe-error', code: error.code, message: error.message }); process.exitCode = 1; });
}

async function tcp(host, port) {
  return new Promise(resolve => {
    const socket = net.connect({ host, port });
    const done = result => { socket.destroy(); resolve(result); };
    socket.once('connect', () => done({ connected: true }));
    socket.once('error', error => done({ connected: false, code: error.code }));
    socket.setTimeout(750, () => done({ connected: false, code: 'TIMEOUT' }));
  });
}
async function main() {
  const [mode, operation, extra] = process.argv.slice(2);
  if (mode === 'child') {
    const child = spawnSync(process.execPath, [__filename, 'direct', operation, extra || ''], { env: process.env, timeout: 10000, encoding: 'utf8' });
    process.stdout.write(child.stdout || '');
    process.stderr.write(child.stderr || '');
    emit({ event: 'child-exit', status: child.status, signal: child.signal });
    process.exitCode = child.status === 0 ? 0 : 1;
    return;
  }
  if (operation === 'privileges') {
    const failures = [];
    for (const uid of [0, Number(extra)]) {
      try { process.setuid(uid); failures.push(`setuid-${uid}-succeeded`); }
      catch (error) { if (error.code !== 'EPERM') failures.push(`setuid-${uid}-${error.code}`); }
    }
    try { process.setgid(0); failures.push('setgid-root-succeeded'); }
    catch (error) { if (error.code !== 'EPERM') failures.push(`setgid-${error.code}`); }
    const sudo = spawnSync('/usr/bin/sudo', ['-n', '/usr/bin/id', '-u'], { encoding: 'utf8', timeout: 3000, env: process.env });
    if (sudo.status !== 1) failures.push(`sudo-status-${sudo.status}`);
    const policy = spawnSync('/sbin/pfctl', ['-a', process.env.PF_ANCHOR, '-f', '/dev/null'], { encoding: 'utf8', timeout: 3000, env: process.env });
    if (policy.status === 0 || !/Permission denied|Operation not permitted/i.test(policy.stderr || '')) failures.push('policy-permission-not-proven');
    emit({ event: 'privileges', failures, sudo_status: sudo.status, policy_status: policy.status });
    if (failures.length) process.exitCode = 1;
    return;
  }
  if (operation === 'positive') {
    const results = [];
    for (const host of ['127.0.0.1', '::1']) {
      const server = http.createServer((_req, response) => response.end('synthetic-ok'));
      server.listen(0, host);
      await once(server, 'listening');
      try {
        const url = `http://${host === '::1' ? '[::1]' : host}:${server.address().port}`;
        const response = await fetch(url, { signal: AbortSignal.timeout(2000) });
        results.push({ host, passed: await response.text() === 'synthetic-ok' });
      } finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
    }
    const path = `${process.env.HOME}/fixture-${process.pid}.sock`;
    const server = net.createServer(socket => socket.end('ok'));
    server.listen(path); await once(server, 'listening');
    try {
      const socket = net.connect(path); await once(socket, 'connect'); socket.destroy();
      results.push({ host: 'unix', passed: true });
    } finally { await new Promise(resolve => server.close(resolve)); if (fs.existsSync(path)) fs.unlinkSync(path); }
    emit({ event: 'positive', results });
    if (results.some(result => !result.passed)) process.exitCode = 1;
    return;
  }
  const family = operation.endsWith('6') ? 6 : 4;
  const local = operation.startsWith('local');
  const protocol = operation.includes('tcp') ? 'tcp' : operation.includes('dns') ? 'dns' : 'udp';
  let host = family === 6 ? '2001:db8::559' : '198.51.100.59';
  let fixture;
  let received = 0;
  let port = protocol === 'dns' ? 53 : 45559;
  if (local) {
    const address = Object.values(os.networkInterfaces()).flat().find(item => !item.internal && item.family === (family === 6 ? 'IPv6' : 'IPv4') && !item.address.startsWith('fe80:'));
    if (!address) { emit({ event: 'negative', operation, unsupported: 'no-nonloopback-address' }); process.exitCode = 2; return; }
    host = address.address;
    fixture = protocol === 'tcp' ? net.createServer(socket => { received++; socket.destroy(); }) : dgram.createSocket(`udp${family}`);
    if (protocol === 'tcp') fixture.listen(0, host);
    else { fixture.on('message', () => received++); fixture.bind(0, host); }
    await once(fixture, 'listening');
    port = fixture.address().port;
  }
  let result;
  try {
    if (protocol === 'tcp') result = await tcp(host, port);
    else if (protocol === 'dns') {
      const resolver = new dns.Resolver({ timeout: 750, tries: 1 });
      resolver.setServers([host]);
      result = await new Promise(resolve => resolver.resolve4('traceai-pf-synthetic.invalid', (error, records) => resolve({ code: error?.code, answered: Boolean(records) })));
    } else {
      const socket = dgram.createSocket(`udp${family}`);
      result = await new Promise(resolve => {
        socket.once('error', error => resolve({ code: error.code }));
        socket.send(Buffer.from('traceai-synthetic'), port, host, error => resolve({ code: error?.code, sent_callback: !error }));
      });
      socket.close();
    }
    // Bounded observation only; never retries a connection or datagram.
    await new Promise(resolve => setTimeout(resolve, 50));
    emit({ event: 'negative', operation, result, received });
    if (result.connected || result.answered || received) process.exitCode = 1;
  } finally { if (fixture) await new Promise(resolve => fixture.close(resolve)); }
}
