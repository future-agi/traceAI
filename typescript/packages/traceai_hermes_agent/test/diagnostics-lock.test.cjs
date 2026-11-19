const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { spawn, spawnSync } = require('node:child_process');
const { once } = require('node:events');
const readline = require('node:readline');
const core = require('@traceai/agent-core');
const crossTest = process.env.HERMES_RESOLVER_PYTHON ? test : test.skip;
const adapter = { id: 'hermes', version: '0.1.0', inspect: async () => ({ registered: true, trust: 'confirmed' }) };

async function fixture(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'hermes-diagnostics-'));
  const server = http.createServer((q, r) => { q.resume(); q.on('end', () => { r.setHeader('Content-Type', 'application/json'); r.end('{}'); }); });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(async () => { server.closeAllConnections(); await new Promise(r => server.close(r)); await fs.rm(home, { recursive: true, force: true }); });
  const env = { HOME: home, FI_AGENT_CONFIG_DIR: path.join(home, 'config'), XDG_STATE_HOME: path.join(home, 'state') };
  const options = name => ({ agent: 'hermes', home, env, agentDir: path.join(home, name) });
  const setup = (name, extra = {}) => core.setup({ ...options(name), configureOnly: true,
    otlpEndpoint: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`, projectName: 'coding-agents',
    credentials: { apiKey: `synthetic-${name}`, secretKey: `synthetic-secret-${name}` }, ...extra });
  return { home, env, options, setup };
}

function pythonState(f, name, project) {
  const r = spawnSync(process.env.HERMES_RESOLVER_PYTHON, ['-I', '-c',
    "import json,os,sys; from pathlib import Path; from traceai_hermes_agent._config import resolve; from traceai_hermes_agent._delivery import Delivery; from opentelemetry.sdk.trace.export import SpanExporter; c=resolve(os.environ.get,host=Path(sys.argv[1]),project=Path(sys.argv[2]) if len(sys.argv)>2 else None); d=Delivery(SpanExporter(),c); d.record(exported=11,lastAcceptedAt=d.now()); print(json.dumps({'state':str(c['state']),'binding':c['diagnostic_binding']}))",
    f.options(name).agentDir, ...(project ? [project] : [])], { env: f.env, encoding: 'utf8', timeout: 5000 });
  assert.equal(r.status, 0, r.stderr);
  return JSON.parse(r.stdout);
}

crossTest('Python activation is scoped to native home, account, destination, project and generation', async t => {
  const f = await fixture(t);
  await f.setup('a'); await f.setup('b');
  const state = pythonState(f, 'a');
  const a = await core.inspectStatus({ ...f.options('a'), adapter });
  assert.equal(a.activation, 'OBSERVED'); assert.equal(a.delivery.exported, 11); assert.equal(a.exitCode, 0);
  const b = await core.inspectStatus({ ...f.options('b'), adapter });
  assert.equal(b.activation, 'NOT_OBSERVED'); assert.equal(b.delivery.exported, 0);
  assert.ok(b.issues.includes('ACTIVATION_NOT_OBSERVED')); assert.equal(b.exitCode, 2);
  const bConfig = await core.resolveConfig(f.options('b'));
  assert.notEqual(bConfig.deliveryBinding, state.binding);
  // Even a misplaced/copied record must not qualify the selected runtime.
  const bState = path.join(bConfig.paths.stateDir, `hermes-${bConfig.deliveryBinding}.json`);
  await fs.copyFile(state.state, bState);
  assert.equal((await core.inspectStatus({ ...f.options('b'), adapter })).activation, 'NOT_OBSERVED');
  // The former unbound shared record is never accepted either.
  await core.writeProtected(path.join(bConfig.paths.stateDir, 'hermes.json'), JSON.stringify({ activatedAt: 'legacy', exported: 99 }));
  assert.equal((await core.inspectStatus({ ...f.options('b'), adapter })).delivery.exported, 0);
  const projectA = path.join(f.home, 'project-a'), projectB = path.join(f.home, 'project-b');
  await fs.mkdir(projectA); await fs.mkdir(projectB);
  pythonState(f, 'a', projectA);
  assert.equal((await core.inspectStatus({ ...f.options('a'), projectDir: projectA, adapter })).activation, 'OBSERVED');
  assert.equal((await core.inspectStatus({ ...f.options('a'), projectDir: projectB, adapter })).activation, 'NOT_OBSERVED');
  // Changing only the destination or account invalidates old acceptance.
  await f.setup('a', { projectName: 'different-destination' });
  assert.equal((await core.inspectStatus({ ...f.options('a'), adapter })).activation, 'NOT_OBSERVED');
  await f.setup('a'); pythonState(f, 'a');
  await f.setup('a', { credentials: { apiKey: 'rotated', secretKey: 'rotated-secret' } });
  assert.equal((await core.inspectStatus({ ...f.options('a'), adapter })).activation, 'NOT_OBSERVED');
  pythonState(f, 'a');
  await core.disable(f.options('a')); await core.enable(f.options('a'));
  const enabled = await core.inspectStatus({ ...f.options('a'), adapter });
  assert.equal(enabled.enabled, true); assert.equal(enabled.activation, 'NOT_OBSERVED');
  assert.equal(enabled.delivery.exported, 0); assert.equal(enabled.delivery.lastAcceptedAt, undefined);
  pythonState(f, 'a');
  assert.equal((await core.inspectStatus({ ...f.options('a'), adapter })).activation, 'OBSERVED');
});

async function line(child, expected) {
  const lines = readline.createInterface({ input: child.stdout });
  try {
    const value = await Promise.race([
      once(lines, 'line').then(([s]) => s),
      once(child, 'exit').then(([c]) => { throw new Error(`child exited ${c} before ${expected}`); }),
    ]);
    assert.equal(value, expected);
  } finally { lines.close(); }
}

crossTest('SIGKILL after lock acquisition cannot strand disable or resurrect queued Python exports', { timeout: 25000 }, async t => {
  const f = await fixture(t); await f.setup('a');
  const config = await core.resolveConfig(f.options('a'));
  // An abandoned empty lock from the preview must not strand the upgrade.
  await fs.writeFile(config.paths.manifest + '.lock', '', { mode: 0o600 });
  // This real native exporter holds an already-resolved pre-disable snapshot.
  const pending = spawn(process.env.HERMES_RESOLVER_PYTHON, ['-u', '-I', '-c', `
import os,sys
from pathlib import Path
from traceai_hermes_agent._config import resolve
from traceai_hermes_agent._delivery import Delivery
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
class MustNotSend(SpanExporter):
    def export(self, spans): raise AssertionError('pending export crossed disable fence')
c=resolve(os.environ.get,host=Path(sys.argv[1]))
d=Delivery(MustNotSend(),c)
print('READY',flush=True)
sys.stdin.readline()
assert d.export([object()]) == SpanExportResult.SUCCESS
assert d._state['dropped'] == 1 and d._state['lastError'] == 'DISABLED'
print('FENCED',flush=True)
`, f.options('a').agentDir], { env: f.env, stdio: ['pipe', 'pipe', 'pipe'] });
  t.after(() => pending.kill('SIGKILL'));
  await line(pending, 'READY');
  // Stop an actual disable() writer immediately after its atomic claim, before
  // even the first heartbeat. No synthetic stale lock substitutes for the crash.
  const code = `
const fs=require('node:fs');const mkdir=fs.mkdir;
fs.mkdir=function(p,...args){const cb=args.pop();return mkdir.call(fs,p,...args,(error)=>{
  if(!error && p===${JSON.stringify(config.paths.manifest + '.write-lock')}){process.stdout.write('LOCKED\\n');return;}
  cb(error);
});};
setInterval(()=>{},1000);
require(${JSON.stringify(require.resolve('@traceai/agent-core'))}).disable(${JSON.stringify(f.options('a'))}).catch(()=>process.exit(3));`;
  const writer = spawn(process.execPath, ['-e', code], { stdio: ['ignore', 'pipe', 'pipe'] });
  t.after(() => writer.kill('SIGKILL'));
  await line(writer, 'LOCKED');
  const killed = once(writer, 'exit'); writer.kill('SIGKILL'); await killed;
  const started = Date.now();
  await core.disable(f.options('a'));
  assert.ok(Date.now() - started < 12000, 'stale recovery is bounded');
  assert.equal((await core.resolveConfig(f.options('a'))).enabled, false);
  await assert.rejects(fs.stat(config.paths.manifest + '.write-lock'), { code: 'ENOENT' });
  await core.enable(f.options('a')); // old runtime must remain fenced after enable
  const status = await core.inspectStatus({ ...f.options('a'), adapter });
  assert.equal(status.activation, 'NOT_OBSERVED');
  const fenced = line(pending, 'FENCED'), exited = once(pending, 'exit');
  pending.stdin.end('flush\n'); await fenced;
  assert.equal((await exited)[0], 0);
  await f.setup('a'); // shared write lock used by setup is healthy too
});
