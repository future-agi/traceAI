const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { resolveConfig } = require('@traceai/agent-core');
const { opencodeV1Adapter: adapter } = require('../dist/opencode-v1.js');
const { loadAdapter } = require('../dist/registry.js');

async function fixture(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'opencode-installer-'));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const host = path.join(home, '.config/opencode'); await fs.mkdir(host, { recursive: true, mode: 0o700 });
  const bin = path.join(home, 'bin'); await fs.mkdir(bin);
  await fs.writeFile(path.join(bin, 'opencode'), '#!/bin/sh\nprintf "1.19.0\\n"\n', { mode: 0o755 });
  const old = process.env.PATH; process.env.PATH = `${bin}:${old}`; t.after(() => process.env.PATH = old);
  const config = await resolveConfig({ agent: 'opencode-v1', home, env: {} });
  return { home, host, ctx: { config, scope: 'user' } };
}
test('CLI selects the actual V1 lifecycle, not an empty slot', () => assert.equal(loadAdapter('opencode-v1'), adapter));
test('setup preserves JSONC comments, unrelated settings and duplicate-free registration', async t => {
  const { host, ctx } = await fixture(t), file = path.join(host, 'opencode.jsonc');
  await fs.writeFile(file, '{\n // keep this\n "plugin": [\n // plugin comment\n "other-plugin"\n ],\n "model": "mock/test",\n}\n', { mode: 0o644 });
  assert.equal((await adapter.install(ctx)).registered, true);
  assert.equal((await adapter.install(ctx)).registered, true);
  const text = await fs.readFile(file, 'utf8');
  assert.ok(text.includes('// keep this')); assert.ok(text.includes('// plugin comment')); assert.ok(text.includes('"model": "mock/test"'));
  assert.equal(text.split('@traceai/opencode-v1-plugin').length - 1, 1);
  assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
  assert.equal((await adapter.uninstall(ctx)).registered, false);
  const after = await fs.readFile(file, 'utf8'); assert.ok(after.includes('other-plugin')); assert.ok(after.includes('// plugin comment')); assert.ok(!after.includes('@traceai/'));
});
test('uninstall does not remove preexisting unmanaged registration', async t => {
  const { host, ctx } = await fixture(t), file = path.join(host, 'opencode.json');
  const original = JSON.stringify({ plugin: ['@traceai/opencode-v1-plugin@0.1.0'] }); await fs.writeFile(file, original);
  await adapter.install(ctx); await adapter.uninstall(ctx);
  assert.equal(await fs.readFile(file, 'utf8'), original);
});
test('V2 and copied observers are explicit duplicate conflicts', async t => {
  const { host, ctx } = await fixture(t);
  await fs.writeFile(path.join(host, 'opencode.json'), JSON.stringify({ plugin: ['@traceai/opencode-plugin@0.1.0'] }));
  assert.equal((await adapter.install(ctx)).duplicate, true);
  await fs.writeFile(path.join(host, 'opencode.json'), '{}');
  await fs.mkdir(path.join(host, 'plugins')); await fs.writeFile(path.join(host, 'plugins/futureagi-otlp.ts'), '// old observer');
  assert.equal((await adapter.install(ctx)).duplicate, true);
});
test('refuses symlink and malformed host config without rewriting it', async t => {
  const { home, host, ctx } = await fixture(t), file = path.join(host, 'opencode.json');
  await fs.writeFile(file, '{ broken'); await assert.rejects(adapter.install(ctx)); assert.equal(await fs.readFile(file, 'utf8'), '{ broken');
  await fs.unlink(file); const target = path.join(home, 'target'); await fs.writeFile(target, '{}'); await fs.symlink(target, file);
  await assert.rejects(adapter.install(ctx)); assert.equal(await fs.readFile(target, 'utf8'), '{}');
});
test('group or world writable host configuration is refused', async t => {
  const { host, ctx } = await fixture(t), file = path.join(host, 'opencode.json');
  await fs.writeFile(file, '{}'); await fs.chmod(file, 0o666);
  await assert.rejects(adapter.install(ctx)); assert.equal(await fs.readFile(file, 'utf8'), '{}');
});
test('unsupported host version cannot be installed even through update', async t => {
  const { home, host, ctx } = await fixture(t);
  await fs.writeFile(path.join(home, 'bin/opencode'), '#!/bin/sh\nprintf "2.0.0\\n"\n');
  const result = await adapter.install(ctx); assert.equal(result.registered, false); assert.equal(result.hostVersion, '2.0.0');
  await assert.rejects(fs.stat(path.join(host, 'opencode.json')), { code: 'ENOENT' });
});
test('host auto-updates within V1 are accepted, but old and other major lines are not', async t => {
  const { home, ctx } = await fixture(t);
  for (const [version, supported] of [['1.19.1', true], ['1.20.0', true], ['1.100.0', true], ['1.18.9', false], ['2.0.0', false], ['1.20.0-beta', false]]) {
    await fs.writeFile(path.join(home, 'bin/opencode'), `#!/bin/sh\nprintf "${version}\\n"\n`);
    assert.equal((await adapter.detect(ctx)).supported, supported, version);
  }
});
test('user registration works when an external-volume project layer is unsafe', async t => {
  const { home } = await fixture(t);
  const projectDir = path.join(home, 'external-project'); await fs.mkdir(projectDir, { mode: 0o775 }); await fs.chmod(projectDir, 0o775);
  const config = await resolveConfig({ agent: 'opencode-v1', home, env: {}, projectDir });
  assert.ok(config.issues.includes('PROJECT_CONFIG_IGNORED'));
  const ctx = { config, scope: 'user', projectDir };
  assert.equal((await adapter.install(ctx)).registered, true);
  assert.equal((await adapter.inspect(ctx)).registered, true);
  await assert.rejects(adapter.install({ ...ctx, scope: 'project' }), /PROJECT_DIRECTORY_REQUIRED/);
});
test('malformed project settings keep user registration usable with the unified diagnostic', async t => {
  const { home } = await fixture(t);
  const projectDir = path.join(home, 'project');
  await fs.mkdir(path.join(projectDir, '.opencode'), { recursive: true });
  await fs.writeFile(path.join(projectDir, '.opencode/futureagi.json'), '{ malformed');
  const config = await resolveConfig({ agent: 'opencode-v1', home, env: {}, projectDir });
  assert.ok(config.issues.includes('PROJECT_CONFIG_IGNORED'));
  const ctx = { config, scope: 'user', projectDir };
  assert.equal((await adapter.install(ctx)).registered, true);
  assert.equal((await adapter.inspect(ctx)).registered, true);
  await assert.rejects(fs.stat(path.join(projectDir, 'opencode.json')), { code: 'ENOENT' });
});
