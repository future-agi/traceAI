import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm, chmod } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import plugin, { supportedHost } from '../dist/index.js';

for (const unsafeVolume of [false, true]) test(`overlapping V2 activations share one exporter (external volume=${unsafeVolume})`, async t => {
  const directory = await mkdtemp(join(tmpdir(), 'fi-opencode-runtime-'));
  const keys = { FI_AGENT_CONFIG_DIR: directory, FI_API_KEY: 'mock-api', FI_SECRET_KEY: 'mock-secret',
    FI_AGENT_ENABLED: 'true', FI_OTLP_ENDPOINT: 'http://127.0.0.1:1/tracer/v1/traces', XDG_CONFIG_HOME: directory };
  const previous = Object.fromEntries(Object.keys(keys).map(k => [k, process.env[k]]));
  Object.assign(process.env, keys);
  t.after(async () => { for (const [k, v] of Object.entries(previous)) v === undefined ? delete process.env[k] : process.env[k] = v; await rm(directory, { recursive: true, force: true }); });
  const project = join(directory, 'volume', 'project');
  await mkdir(project, { recursive: true });
  if (unsafeVolume) await chmod(join(directory, 'volume'), 0o775);
  let registrations = 0, disposed = 0;
  const hook = async () => { registrations++; return { async dispose() { disposed++; } }; };
  const ctx = { app: { version: '2.2.6' }, location: { directory: project, project: { id: 'synthetic', directory: project, canonical: project } },
    session: { hook }, tool: { hook }, event: { async *subscribe({ signal }) {
      if (!signal.aborted) await new Promise(r => signal.addEventListener('abort', r, { once: true }));
    } } };
  const a = await plugin.setup(ctx), b = await plugin.setup(ctx);
  const runtimes = globalThis[Symbol.for('@traceai/opencode-plugin/v2/runtime')];
  assert.equal(runtimes.size, 1);
  assert.equal((await [...runtimes.values()][0]).refs, 2);
  await a(); assert.equal(runtimes.size, 1);
  await b(); assert.equal(runtimes.size, 0);
  assert.equal(registrations, disposed);
});
test('repository transport with environment keys stays inactive before hooks collect content', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'fi-opencode-untrusted-'));
  const values = { FI_AGENT_CONFIG_DIR: directory, XDG_CONFIG_HOME: directory, FI_AGENT_ENABLED: 'true',
    FI_API_KEY: 'synthetic-env-api', FI_SECRET_KEY: 'synthetic-env-secret', FI_OTLP_ENDPOINT: undefined, FI_BASE_URL: undefined };
  const previous = Object.fromEntries(Object.keys(values).map(k => [k, process.env[k]]));
  for (const [k, v] of Object.entries(values)) v === undefined ? delete process.env[k] : process.env[k] = v;
  t.after(async () => { for (const [k, v] of Object.entries(previous)) v === undefined ? delete process.env[k] : process.env[k] = v; await rm(directory, { recursive: true, force: true }); });
  await mkdir(join(directory, '.opencode'));
  await writeFile(join(directory, '.opencode/futureagi.json'), JSON.stringify({ schemaVersion: 1,
    transport: { endpoint: 'http://127.0.0.1:1/tracer/v1/traces' } }), { mode: 0o600 });
  let collected = false;
  const hook = async () => { collected = true; return { async dispose() {} }; };
  const cleanup = await plugin.setup({ app: { version: '2.2.5' }, location: { directory },
    session: { hook }, tool: { hook }, event: { async *subscribe() { collected = true; } } });
  assert.equal(cleanup, undefined);
  assert.equal(collected, false);
});

test('host updates use the qualified V2 line rather than exact equality', async () => {
  for (const version of ['2.2.5', 'v2.2.6', '2.3.0', '2.99.0']) assert.equal(supportedHost(version), true);
  for (const version of ['1.19.0', '2.2.4', '3.0.0', '2.3.0-preview', 'unknown']) assert.equal(supportedHost(version), false);
  assert.equal(await plugin.setup({ app: { version: '2.3.0' }, session: {}, tool: {}, event: {} }), undefined);
});
