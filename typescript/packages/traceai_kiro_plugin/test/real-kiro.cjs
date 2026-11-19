const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const http = require('node:http');
const { spawn } = require('node:child_process');
const core = require('@traceai/agent-core');
const { kiroAdapter } = require('../dist/index.js');
function run(bin, args, options = {}) {
  return new Promise(resolve => {
    const child = spawn(bin, args, { ...options, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    child.stdout.on('data', c => stdout += c); child.stderr.on('data', c => stderr += c);
    const timer = setTimeout(() => child.kill('SIGKILL'), 15000);
    child.on('close', (code, signal) => { clearTimeout(timer); resolve({ code, signal, stdout, stderr }); });
  });
}
async function main() {
  const home = '/work/native-home', project = '/work/native-project';
  await fs.mkdir(home, { mode: 0o700 }); await fs.mkdir(project, { mode: 0o700 });
  const requests = [];
  const server = http.createServer((req, res) => {
    const chunks = []; req.on('data', x => chunks.push(x)); req.on('end', () => {
      requests.push(JSON.parse(Buffer.concat(chunks).toString()));
      res.setHeader('Content-Type', 'application/json'); res.end('{}');
    });
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  try {
    const env = { PATH: process.env.PATH, HOME: home, DO_NOT_TRACK: '1', AWS_EC2_METADATA_DISABLED: 'true',
      FI_API_KEY: 'synthetic-api', FI_SECRET_KEY: 'synthetic-secret',
      FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/tracer/v1/traces` };
    const cli = process.env.KIRO_TEST_CLI ?? path.resolve(__dirname, '../../traceai_agents/dist/cli.js');
    const setup = await run(process.execPath, [cli, 'setup', 'kiro-cli', '--project', 'native-kiro', '--json', '--project-dir', project], { env, cwd: project });
    assert.equal(setup.code, 0, setup.stdout + setup.stderr);
    assert.equal(JSON.parse(setup.stdout).registered, true);
    const again = await run(process.execPath, [cli, 'setup', 'kiro-cli', '--project', 'native-kiro', '--json', '--project-dir', project], { env, cwd: project });
    assert.equal(again.code, 0);
    const options = { agent: 'kiro-cli', home, env, projectDir: project };
    const config = await core.resolveConfig(options);
    const context = { config, scope: 'user', projectDir: project };
    const detected = await kiroAdapter.detect(context); assert.equal(detected.version, '3.0.0'); assert.equal(detected.supported, true);
    const hooks = JSON.parse(await fs.readFile(path.join(home, '.kiro/hooks/futureagi-kiro-cli.json'), 'utf8'));
    assert.equal(hooks.hooks.length, 6);
    // Real, pinned binary; network syscalls denied in the child and descendants by seccomp.
    const nativeEnv = { HOME: home, PATH: env.PATH, DO_NOT_TRACK: '1', AWS_EC2_METADATA_DISABLED: 'true', TERM: 'dumb' };
    const probe = (args) => run('/usr/bin/python3', ['-I', path.join(__dirname, 'no-network.py'), process.env.KIRO_TEST_BIN, ...args], { env: nativeEnv, cwd: project });
    const help = await probe(['chat', '--help']); assert.equal(help.code, 0);
    const diagnostic = await probe(['diagnostic']);
    const startup = await probe(['chat', '--v3', '--no-interactive', 'Reply with OK. Do not call tools.']);
    const status = await run(process.execPath, [cli, 'status', 'kiro-cli', '--json'], { env, cwd: project });
    const evidence = { host: detected, registration: JSON.parse(setup.stdout), repeatSetup: JSON.parse(again.stdout),
      nativeHelp: help, nativeDiagnostic: diagnostic, nativeStartup: startup, status: JSON.parse(status.stdout),
      nativeSpanCount: requests.flatMap(x => x.resourceSpans ?? []).length,
      gate: 'HOLD: native help/registration are not a real-model tracing pass; service authentication and a permitted model route remain required',
      networkPolicy: 'AF_INET and AF_INET6 sockets denied by kernel seccomp for native subprocesses; no keys or service calls' };
    await fs.writeFile('/work/evidence/native.json', JSON.stringify(evidence, null, 2));
    console.log(JSON.stringify(evidence, null, 2));
    assert.equal(evidence.nativeSpanCount, 0);
  } finally { server.closeAllConnections(); await new Promise(r => server.close(r)); }
}
main().catch(e => { console.error(e); process.exitCode = 1; });
