// Dependency-only broker stays outside the test's network namespace.
// The isolated real agent can reach only its loopback model/OTLP/registry servers.
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { unlink } from 'node:fs/promises';
const socket = '/work/npm-fixture.sock';
await unlink(socket).catch(() => {});
const broker = createServer(async (req, res) => {
  try {
    const path = new URL(req.url, 'http://fixture').pathname;
    if (!path.startsWith('/npm/')) { res.writeHead(403); res.end(); return; }
    const response = await fetch('https://registry.npmjs.org/' + path.slice(5));
    res.writeHead(response.status, { 'content-type': response.headers.get('content-type') ?? 'application/octet-stream' });
    res.end(Buffer.from(await response.arrayBuffer()));
  } catch { res.writeHead(502); res.end(); }
});
await new Promise(resolve => broker.listen(socket, resolve));
const child = spawn('unshare', ['--net', 'bash', '-c', 'ip link set lo up; exec node /work/typescript/packages/traceai_opencode_v1_plugin/test/e2b.mjs'], { stdio: 'inherit' });
child.on('exit', code => { broker.close(); broker.closeAllConnections(); process.exitCode = code ?? 1; });
