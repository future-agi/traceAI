const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { promisify } = require('node:util');
const execFile = promisify(require('node:child_process').execFile);
const { adapterRegistry, loadAdapter } = require('../dist/registry.js');
const ids = ['muse', 'gemini', 'kiro-cli', 'kiro-ide', 'augment', 'augment-ide', 'devin-cascade'];

test('held registry keeps distinct surfaces and both Kiro package entries', () => {
  for (const id of ids) {
    const entries = adapterRegistry.filter(a => a.id === id);
    assert.equal(entries.length, 1, id);
    assert.equal(entries[0].qualified, false, id);
    assert.equal(loadAdapter(id).id, id);
  }
  assert.equal(require('@traceai/kiro-plugin').kiroAdapter.id, 'kiro-cli');
  assert.equal(require('@traceai/kiro-plugin/ide').kiroIdeAdapter.id, 'kiro-ide');
});

test('held GUI activation refuses before credentials or filesystem writes', async t => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'fi-held-gates-'));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  for (const id of ['kiro-ide', 'augment-ide', 'devin-cascade']) {
    const expected = adapterRegistry.find(a => a.id === id).setupHold;
    for (const command of ['setup', 'enable', 'update', 'run']) {
      const result = await execFile(process.execPath, [path.resolve(__dirname, '../dist/cli.js'), command, id, '--json'], {
        cwd: home, env: { HOME: home, PATH: '/no-host-binaries', FI_OTLP_ENDPOINT: 'http://127.0.0.1:1/traces' }, timeout: 5000,
      }).then(() => { throw new Error('held activation succeeded'); }, error => error);
      assert.equal(result.code, 2);
      assert.equal(result.stderr, '');
      assert.equal(JSON.parse(result.stdout).code, expected);
      assert.deepEqual(await fs.readdir(home), []);
    }
  }
});

test('adapter outer lease waits without deadlocking protected state writes', async t => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'fi-held-lease-'));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const { acquireWriteLock } = require('@traceai/agent-core/file-lock');
  const { lockState } = require('../../traceai_augment_plugin/dist/state.js');
  const { writeProtected, readProtected } = require('@traceai/agent-core');
  const target = path.join(home, 'a'.repeat(64) + '.json');
  // Previous implementations used O_EXCL files; those cannot fence current writers.
  await fs.writeFile(target + '.mutex', '', { mode: 0o600 });
  const first = await acquireWriteLock(target + '.event');
  const release = new Promise((resolve, reject) => setTimeout(() => first.release().then(resolve, reject), 100));
  const unlock = await lockState(target);
  try { await writeProtected(target, '{}'); } finally { await unlock(); }
  await release;
  assert.equal(await readProtected(target), '{}');
  assert.equal((await fs.stat(target + '.mutex')).isFile(), true);
});
