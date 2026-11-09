const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { execFile } = require('node:child_process');
const c = require('@traceai/agent-core');
const { opencodeAdapter, OPENCODE_PACKAGE, supportedHost } = require('../dist/adapters/opencode.js');

test('status and doctor surface current host failures despite historical activation', async t => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'fi-opencode-health-'));
  const bin = path.join(home, 'bin'), host = path.join(home, '.config/opencode');
  await fs.mkdir(bin); await fs.mkdir(host, { recursive: true });
  const server = http.createServer((req, res) => { req.resume(); req.on('end', () => { res.setHeader('content-type', 'application/json'); res.end('{}'); }); });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const oldPath = process.env.PATH;
  process.env.PATH = `${bin}:${oldPath}`;
  t.after(async () => { process.env.PATH = oldPath; server.closeAllConnections(); await new Promise(r => server.close(r)); await fs.rm(home, { recursive: true, force: true }); });
  const env = { HOME: home, XDG_CONFIG_HOME: path.join(home, '.config'), XDG_STATE_HOME: path.join(home, '.local/state') };
  const options = { agent: 'opencode', home, env };
  await c.setup({ ...options, configureOnly: true, credentials: { apiKey: 'synthetic-api', secretKey: 'synthetic-secret' }, otlpEndpoint: `http://127.0.0.1:${server.address().port}/tracer/v1/traces` });
  await fs.writeFile(path.join(host, 'opencode.json'), JSON.stringify({ plugins: [OPENCODE_PACKAGE] }), { mode: 0o644 });
  const config = await c.resolveConfig(options);
  const tracer = c.createTurnTracer({ config });
  tracer.startTurn({ sessionId: 's', turnId: 'old' }).end(); await tracer.shutdown();
  const file = path.join(bin, 'opencode');
  const fakeHost = async version => fs.writeFile(file, `#!${process.execPath}\nprocess.stdout.write(${JSON.stringify('opencode ')} + ${JSON.stringify(version)} + '\\n');\n`, { mode: 0o755 });
  for (const version of ['2.2.5', '2.2.6', '2.3.0']) {
    await fakeHost(version);
    const status = await c.inspectStatus({ ...options, adapter: opencodeAdapter, check: true });
    assert.equal(supportedHost(version), true); assert.equal(status.exitCode, 0); assert.equal(status.hostVersion, version);
  }
  for (const [version, issue] of [['3.0.0', 'UNSUPPORTED_HOST_VERSION'], [null, 'HOST_NOT_FOUND']]) {
    if (version) await fakeHost(version); else await fs.unlink(file);
    // Empty host PATH prevents accidental discovery of a global OpenCode binary.
    const hostPath = version ? bin : path.join(home, 'missing-bin');
    process.env.PATH = hostPath;
    const status = await c.inspectStatus({ ...options, adapter: opencodeAdapter, check: true });
    assert.equal(status.registration, 'REGISTERED');
    assert.equal(status.activation, 'OBSERVED', 'timestamp stays visible as historical evidence');
    assert.ok(status.issues.includes(issue)); assert.equal(status.exitCode, 2);
    for (const command of ['status', 'doctor']) {
      const result = await new Promise(resolve => execFile(process.execPath, [path.join(__dirname, '../dist/cli.js'), command, 'opencode', '--check', '--json'], { env: { ...env, PATH: hostPath }, cwd: home }, (error, stdout) => resolve({ code: error?.code ?? 0, status: JSON.parse(stdout) })));
      assert.equal(result.code, 2); assert.ok(result.status.issues.includes(issue));
    }
  }
});
